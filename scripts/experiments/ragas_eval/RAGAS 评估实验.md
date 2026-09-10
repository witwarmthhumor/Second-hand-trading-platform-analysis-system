# RAGAS 评估实验：RAG 体系评估 → 优化 → 复测

对当前二手交易知识库 RAG 体系做 RAGAS 评估，定位瓶颈并落地一项优化，复测对比。

## 一、为什么用「非 LLM」指标

RAGAS 有两类指标：

| 类别 | 指标 | 是否需 LLM |
|------|------|-----------|
| 检索侧 | context_precision / context_recall | 有非 LLM 版本（字符串相似度） |
| 生成侧 | faithfulness / answer_relevancy / answer_correctness | **必须 LLM 评判** |

本实验起步时本项目 LLM 网关（rightapi）不可用（`gpt-5.6-luna` 报 `503
Pricing configuration is temporarily unavailable`，后续整账户余额耗尽
`403 余额不足`），因此生成侧指标当时无法运行。

> **补充（后已打通）**：LLM 通道最终切至 DeepSeek（`api.deepseek.com/v1` +
> `deepseek-chat`，非思考型模型）后，生成侧 LLM 指标已补测完成，见**第七节**。

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

> ⚠️ **后续修正（见第九节）**：上表中「文档去重（diverse）无益」的结论**已被推翻**。
> 当时只看 doc 级 Hit@5（只问「至少中一篇」），换用「多文档覆盖率」后，diverse
> 反而是收益最大的一项（0.376→0.52~0.55），且代价极小。指标选错会得出反向结论。

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

## 七、生成侧评估：命中直出原文 vs LLM 合成

rerank 把 doc 级检索推到满分后，下一步验证**生成环节**：生产 `ask()` 在
命中时「直接吐 top1 片段原文」，而 RAG 的常规做法是「让 LLM 基于 top-k 片段
合成一段答案」。两者谁更好？

### 7.1 实验设置

| 项 | 取值 |
|----|------|
| 样本 | 40 问分层抽样（单文档 20 / 多文档 20） |
| 检索 | 生产默认（`RERANK_BY_VSIM=True`），top_k=5 |
| 直出原文（基线） | `answer_direct(results[0]["text"])`，即生产 `ask()` 命中路径 |
| 合成 v1 | LLM 基于 top-5 合成，**正文内联**`【来源:doc_id】` |
| 合成 v2 | LLM 基于 top-5 合成，来源**末尾集中**列出，行文更自然 |
| 裁判 | RAGAS faithfulness / answer_relevancy（LLM）+ 来源覆盖度（chunk 级） |
| 成本 | 320 次调用 / 入 38.6 万 + 出 22.2 万 tokens ≈ **¥1.36**（DeepSeek 低谷半价） |

> **基线天花板效应（必读）**：直出答案就是片段原文，`faithfulness` 天然 ≈1。
> 因此「合成后 faithfulness 下降」是**结构性必然**，不能直接读成「合成在编造」；
> 判据要看绝对水平（本实验 ≥0.94）与是否存在低分样本。

### 7.2 总体对比

| 指标 | 直出原文（基线） | 合成·v1 内联来源 | 合成·v2 来源末尾集中 |
|------|------------------|------------------|----------------------|
| faithfulness（忠于片段） | 0.9944 | 0.9682（↓2.6pp） | 0.9439（↓5.1pp） |
| answer_relevancy（答到点上） | 0.8455 | 0.8986（↑5.3pp） | **0.9084**（↑6.3pp） |
| 来源覆盖度 | 0.6025 | **0.7000**（↑9.8pp） | **0.7000**（↑9.8pp） |
| 答案平均字数 | 317.1 | 678.6 | 602.6 |
| 平均生成耗时 | 0s（零 LLM） | 2.02s | 1.97s |

**一句话结论**：合成换来 **+9.8pp 覆盖度 / +5.3~6.3pp 相关性**，代价是
**-2.6~-5.1pp 忠实度**，且每次多 ~2 秒 LLM 延迟。

### 7.3 分层对比（多文档是真正的短板）

| 指标 | 直出原文 | 合成·v1 | 合成·v2 |
|------|----------|---------|---------|
| 覆盖度 · 单文档 | 0.9000 | **1.0000** | **1.0000** |
| 覆盖度 · 多文档 | 0.3050 | 0.4000 | 0.4000 |
| 相关性 · 单文档 | 0.8084 | 0.9066 | **0.9150** |
| 相关性 · 多文档 | 0.8826 | 0.8905 | **0.9018** |
| 忠实度 · 单文档 | 0.9890 | 0.9694 | 0.9596 |
| 忠实度 · 多文档 | 1.0000 | 0.9671 | 0.9283 |

逐问翻转（answer_relevancy，相对直出基线）：

- 合成 v1：变好 **25** / 变差 14 / 持平 1
- 合成 v2：变好 **25** / 变差 13 / 持平 2
- 两个风格均**无** `faithfulness < 0.6` 的样本 → 未见明显编造。

### 7.4 归因：提升与代价分别来自哪

| 现象 | 归因 |
|------|------|
| 覆盖度大幅提升（+9.8pp） | 直出只给 top1 一篇，**天生只覆盖期望文档的一部分**；合成能看到 top-5，把多篇内容拼进来 |
| **多文档覆盖度仍只有 0.40** | top-5 检索本身只覆盖多文档期望文档的 **~42%**（285 问全量实测 **0.3755**，见第九节）——LLM 无法合成它**没检索到**的内容。**短板一半在检索（召回不全）、一半在生成（组织不全）** |
| 单文档相关性提升最大（+10.7pp） | 直出单篇原文常「只回答了一半问题」；LLM 能据问补全 |
| v1 忠实度高于 v2（0.9682 vs 0.9439） | v1 要求**边写边挂来源**，模型全程被来源约束；v2 允许先自由行文、末尾补来源，自由度更高→更流畅也更易漂移 |
| v2 相关性略高于 v1（0.9084 vs 0.8986） | 同一原因：自由度换来的行文自然度 |

**⚠️ 对第六节结论的修正**：第六节说「检索侧召回已到上限」。严格讲，那只对
**doc 级 Hit@5=1.0（至少命中一篇）**成立。本节的 0.40 显示：**多文档场景
「期望的 N 篇是否都进 top-5」的覆盖率仍很低**——检索的上限只保证了「不空手」，
没保证「拿全」。

### 7.5 落地建议

1. **若要上线合成，选 v1（内联来源）**：忠实度更高（0.9682 vs 0.9439），
   相关性仅低 1pp，且来源可随文点验，便于人工核对与溯源。
2. **直出原文仍是安全默认**：零延迟、零成本、忠实度≈1；对「答案就在一篇
   文档里」的单文档问（覆盖度可达 1.00）已足够，无需为 +5pp 相关性付 2 秒延迟。
3. **真正的下一步是提升多文档召回**（而非继续调生成）：候选池扩大、BM25
   加权、文档去重在本语料已验证无效，需换思路——**query decomposition
   （把多文档问题拆成子问题分别检索）**或 **按文档保底各取 1 篇**。
4. 评测集需补 **chunk 级 golden**，否则覆盖度（分母为期望文档的全部 chunk）
   口径偏严，绝对分偏低。

## 八、下一步建议

| 优先级 | 方向 | 说明 |
|--------|------|------|
| **P0** | **多文档召回：文档级去重 + 补位** | 第九节实测 diverse50/80 可把多文档 coverage 0.376→0.52~0.55，**且不牺牲 doc 级命中**；见 9.4 的「保底+补位」形态 |
| P1 | 合成答案灰度上线 | 用 v1 风格，先在多文档问题上开（单文档无必要），加「来源可点验」 |
| P2 | chunk 级 golden | 补细粒度标注，让 recall / 覆盖度口径可信 |
| P3 | 成本与延迟 | 合成每问 +2s、+¥0.017；若全量铺开需评估缓存复用（同问同片段走缓存） |

## 九、多文档召回诊断：推翻「diverse 无效」的旧结论

### 9.1 起因：此前用错了指标

第四节消融表里，`diverse50`（每文档最多保留 1 个 chunk）被判为**无效**，
依据是 doc 级 Hit@5 从 0.9825 掉到 0.9754。但 **Hit@5 只回答「期望文档里
至少有一篇进 top-5」**——它不惩罚「只中 1 篇、漏掉 4 篇」。指标选错，结论就反了。

补测两个更贴多文档场景的指标（脚本 `coverage_by_param.py`，按**生产 top_k=5**）：

- `coverage@5`：top-5 命中的期望文档数 / 期望文档总数
- `distinct_docs@5`：top-5 里不同 doc_id 的个数（衡量「同文档多 chunk 占位」）

> 口径提醒：`run_eval.py` 默认 `top_k=20`，而本诊断按 `top_k=5`（=生产），
> 两者**不可直接比较**。生产 rerank 的实现是「先取 RRF top-5，再在这 5 个里
> 按 vsim 重排」，top_k 不同会改变 rerank 的输入集合。

### 9.2 结果（285 问：多文档 122 / 单文档 163）

| 参数组 | 多文档 coverage@5 | distinct_docs@5 | 单文档 coverage | Hit(任意一篇) |
|--------|-------------------|-----------------|-----------------|---------------|
| baseline | 0.3755 | 2.84 | 0.9755 | 0.9825 |
| **rerank（当前生产）** | **0.3755** | **2.84** | 0.9755 | 0.9825 |
| pool50 | 0.3865 | 2.85 | 0.9632 | 0.9719 |
| **diverse50** | **0.5187** | **5.00** | 0.9693 | 0.9754 |
| **diverse80** | **0.5474** | **5.00** | 0.9571 | 0.9684 |

### 9.3 三条硬结论

1. **diverse 把多文档覆盖率从 0.376 抬到 0.519 / 0.547（+14.3~17.2pp）**，代价
   仅 doc 级 Hit(任意一篇) -0.7~-1.4pp、单文档 coverage -0.6~-1.8pp。此前判
   「无效」纯属**指标选错**。
2. **rerank 对多文档覆盖零贡献（0.3755 → 0.3755，distinct 2.84 完全一致）**。
   因为它只重排、不加新成员：**改顺序不改成员**。它贡献的是 doc 级 MRR/nDCG
   （把正确文档排前面），不是覆盖。
3. **扩候选池（pool50）几乎无效（0.376→0.387）** → 瓶颈不在「候选不够」，
   而在**排序阶段的文档聚簇**：多文档问题的 top-5 平均只落在 **2.84 个**不同
   文档上（而多文档问题平均期望 7 篇以上）。

### 9.4 生产接入的正确形态：保底 + 补位

直接上 `diverse50` 会牺牲单文档表现（单文档 coverage 0.9755→0.9693）。
更稳的做法是把去重从「硬截断」改成「保底 + 补位」：

1. 按「每文档只取 RRF 最高的 chunk」去重排序 → 得到文档级候选；
2. 取前 `min(top_k, 文档数)` 篇；
3. 若命中文档数不足 `top_k`（单文档/双文档问题常见），剩余名额回填**同文档的
   次优 chunk**（按 vsim）。

这样：多文档问题最大化文档覆盖；单文档问题仍填满 top_k、不损失原有表现；且
`results[0]` 仍是全局最相似 chunk，**命中判定口径（`vector_sim >= 阈值`）不变**。

## 复现

```bash
cd <项目根目录>

# ---------- 检索侧（第一~六节） ----------
# 1. 基线（参数组 baseline，等价 rerank 关闭）
backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_eval.py \
    --params baseline --strategy baseline --out baseline.json

# 2. 优化后（参数组 rerank = 当前生产默认）
backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_eval.py \
    --params rerank --strategy rerank --out optimized_prod.json

# 3. 对比
backend/.venv/Scripts/python scripts/experiments/ragas_eval/compare.py

# ---------- 生成侧（第七节） ----------
# 需在 backend/.env 配好 LLM_API_BASE / LLM_API_KEY / LLM_MODEL
# 4. 直出原文基线（40 问分层抽样；输出自动命名 gen_direct.json）
backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_gen_eval.py \
    --sample 40 --skip-synth

# 5. 合成 v1 / v2 消融（断点续跑，¥2 成本闸门，超预算退出码 3）
backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_gen_eval.py \
    --sample 40 --synth-styles v1,v2 --skip-direct --resume --budget-cny 2.0

# 6. 生成侧对比表
backend/.venv/Scripts/python scripts/experiments/ragas_eval/compare_gen.py

# ---------- 多文档召回诊断（第九节） ----------
# 7. 各参数组在 top_k=5（生产口径）下的多文档覆盖率
backend/.venv/Scripts/python scripts/experiments/ragas_eval/coverage_by_param.py \
    --params baseline,rerank,pool50,diverse50,diverse80
```

> 依赖：`ragas==0.3.5` + `rapidfuzz`（非 LLM 指标需 rapidfuzz 做字符串相似度）。
> 生成侧另需可用的 OpenAI 兼容 LLM 通道（本项目实测 `deepseek-chat` 可用，
> 非思考型、不需大 `max_tokens`；若换思考型模型如 `gemini-3.x-flash`，
> `max_tokens` 需 ≥3000，否则正文被思考 token 挤掉）。
> 结果文件：`results/{baseline,optimized_prod,gen_direct,gen_synth_v1,gen_synth_v2,coverage_by_param}.json`。
