# RAGAS 评估实验：RAG 体系评估 → 优化 → 复测

对当前二手交易知识库 RAG 体系做 RAGAS 评估，定位瓶颈并落地一项优化，复测对比。

## 一、为什么用「非 LLM」指标

RAGAS 有两类指标：

| 类别 | 指标 | 是否需 LLM |
|------|------|-----------|
| 检索侧 | context_precision / context_recall | 有非 LLM 版本（字符串相似度） |
| 生成侧 | faithfulness / answer_relevancy / answer_correctness | **必须 LLM 评判** |

本项目 LLM 网关（rightapi）当前不可用（`gpt-5.6-luna` 报 `503 Pricing
configuration is temporarily unavailable`），因此生成侧指标无法运行。

本实验选用两个**非 LLM** 检索指标，正好贴合「先优化召回」的目标：

- **`NonLLMContextPrecisionWithReference`**：衡量「相关块是否排在前面」，
  用 rapidfuzz 字符串相似度判断 retrieved 与 reference 是否匹配。
- **`NonLLMContextRecall`**：衡量「该命中的内容有没有被漏掉」。

同时保留本项目既有的 **Hit@5 / MRR@10 / nDCG@10**（doc 级）作为对照互证。

## 二、评估口径与一个方法论局限

**评测集**：`rag_data/eval/golden_queries.yaml`（285 问），`expected_ids` 为
**文档级**标注（如 `A-001` / `FAQ-049`）。

**reference 构造**：把每个 expected 文档展开为它的**分块列表**（与 retrieved
同粒度），否则 rapidfuzz 字符串相似度会因长度差异失真（900 字 chunk 对比
3000 字全文会被系统性低估）。

**关键局限（务必知悉）**：golden 是文档级标注，但 RAGAS 非 LLM 指标是
**chunk 级**字符串匹配。FAQ 文档按「条」分块（一个文档 24 条 = 24 个 chunk），
但一个查询往往只精确对应其中 1~2 条。把 expected 文档展开成全部 chunk 后，
`context_recall` 的分母被虚高，导致**绝对 recall 值系统性偏低**（尤其多文档
问题：expected 5 个文档 × 24 chunk = 120 条 reference，但检索只返回 top-20）。

因此：

- `context_recall` 的**绝对低值**是口径 artifact，不代表真实漏检（doc 级
  Hit@5=0.98 证明文档找得到）。
- `context_recall` 在**优化前后同口径对比**下仍有参考价值，但本实验的主
  要结论以 **context_precision + Hit@5/MRR/nDCG** 为准。

## 三、基线诊断

285 问基线（优化前，生产默认检索）：

| 指标 | 基线值 | 解读 |
|------|--------|------|
| context_precision | 0.7599 | 排序质量：相关块约 76% 排得靠前 |
| context_recall | 0.6179 | 含口径 artifact，见上 |
| Hit@5 | 0.9825 | doc 级命中已很高 |
| MRR@10 | 0.9186 | — |
| nDCG@10 | 0.7960 | — |

**瓶颈定位**：抽查发现，RRF 融合后混入了大量 **BM25 独有命中、向量相似度为 0**
的 chunk（纯关键词匹配、语义无关），它们被 RRF 排到靠前位置，把真正语义相关
的 chunk 挤下去。这是 context_precision 未满的根本原因。

例：查询「卖二手时发货时限要求有什么讲究？」的 RRF top8 中，第 2/4/6 位是
`D-161`、`C-006`、`FAQ-013`，三者 `vector_sim=0.0`（纯 BM25 命中）。

## 四、优化方案：rerank 精排

**改动**：RRF 融合得到候选后，按**向量相似度（vsim）二次排序**，把 vsim≈0
的纯 BM25 噪声沉到末尾。

```python
# retriever.py search() 内，RRF 排序后
if RERANK_BY_VSIM:
    ranked = sorted(ranked, key=lambda x: -v_scores.get(x[0], 0.0))[:top_k]
```

- 落地点：`backend/app/rag/retriever.py`（`RERANK_BY_VSIM=True`，见
  `config.py`）
- 实验验证：`scripts/experiments/ragas_eval/strategies.py` 里 `rerank` /
  `rerank_pool50` 两组消融

**消融过程**（均 285 问全量）：

| 参数组 | 说明 | Hit@5 | MRR@10 | context_precision |
|--------|------|-------|--------|-------------------|
| baseline | 生产默认（QUERY_N=20） | 0.9825 | 0.9186 | 0.7599 |
| pool50 | 候选池 20→50 | 0.9754 | 0.8951 | 0.7499 |
| pool50_bm2 | 候选池 50 + BM25 加权 2× | 0.9158 | 0.8633 | 0.7597 |
| diverse50 | 候选池 50 + 文档去重 | 0.9754 | 0.8951 | 0.7578 |
| **rerank** | RRF 后 vsim 精排 | **1.0000** | **0.9506** | **0.7682** |
| rerank_pool50 | rerank + 候选池 50 | 1.0000 | 0.9506 | 0.7671 |

结论：**扩大候选池、BM25 加权、文档去重均无益甚至有害**；唯一有效的是
**rerank 精排**——因为它针对的是真正的病根（BM25 噪声污染排序）。

## 五、优化前后对比

| 指标 | 优化前 | 优化后 | Δ |
|------|--------|--------|-----|
| **Hit@5** | 0.9825 | **1.0000** | **+0.0175（满分）** |
| **MRR@10** | 0.9186 | **0.9506** | **+0.0320** |
| **nDCG@10** | 0.7960 | **0.8280** | **+0.0320** |
| context_precision | 0.7599 | 0.7682 | +0.0083 |
| context_recall | 0.6179 | 0.6179 | 0（口径 artifact，见第二节） |

## 六、归因：提升来自哪

| 维度 | 变化 | 说明 |
|------|------|------|
| doc 级命中 | Hit@5 满分、MRR +3.2pp | 压掉 BM25 噪声后，真正相关的 chunk 排到前 5 |
| 单文档 precision | 0.7738 → 0.7916 | 排序更准，直接受益 |
| 多文档 precision | 0.7414 → 0.7370 | 略降（BM25 在多关键词问题上本有帮助） |
| recall | 不变 | 与 rerank 无关，受口径 artifact 主导 |

**核心机制**：RRF 是排名融合，只保留「名次」信息、丢弃「相似度大小」。当
BM25 对某个 chunk 给出高关键词匹配（但语义无关、vsim=0）时，它会被 RRF
排到靠前。rerank 用 vsim 二次排序，等价于「让语义相似度重新主导排序」，
压掉了这类纯关键词噪声。

**收益边界**：doc 级指标（Hit@5/MRR/nDCG）全面大幅提升；chunk 级
context_precision 小幅提升（+0.8pp）。多文档问题的 chunk 级 precision 略降
（-0.4pp），属可接受的范围——因为 doc 级命中才是本系统「命中即直出原文」
问答链路的关键。

## 七、下一步建议

rerank 已把 doc 级命中推到满分，**检索侧召回已到上限**。后续若要再提升
端到端质量，方向在**生成侧**（当前 LLM 网关不可用，暂无法测）：

1. **生成侧 RAGAS 评估**：待 LLM 恢复后，测 faithfulness / answer_relevancy，
   验证「命中直出原文」vs「LLM 生成」的答案质量差异。
2. **多文档聚合的上下文组织**：多文档问题的 chunk 级 recall 受口径影响，
   真实瓶颈可能在「如何把 5 个相关文档的内容组织成完整答案」，而非检索。
3. **细粒度评测集**：若要严谨测 chunk 级 recall，需补一份 chunk 级（而非
   doc 级）的 golden 标注。

## 复现

```bash
cd <项目根目录>

# 1. 基线（需先临时关闭 rerank：config.py 里 RERANK_BY_VSIM=False）
backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_eval.py \
    --strategy baseline --out baseline.json

# 2. 优化后（RERANK_BY_VSIM=True，当前默认）
backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_eval.py \
    --strategy optimized_prod --out optimized_prod.json

# 3. 对比
backend/.venv/Scripts/python scripts/experiments/ragas_eval/compare.py
```

> 依赖：`ragas==0.3.5` + `rapidfuzz`（非 LLM 指标需 rapidfuzz 做字符串相似度）。
