# RAG 优化方案（多文档召回 · 兜底路径 · 生成侧上线）

> 生成时间：2026-09-11
> 依据：`scripts/experiments/ragas_eval/` 全部实测（285 问金标准 + 40 问生成侧分层抽样）、
> `coverage_by_param.py` 多文档覆盖诊断、`RAGAS 评估实验.md` 第一~十二节。
> 定位：**可执行方案**。每条给出改动点、实测依据、验收标准、回滚方式。
> 优先级：P0 = 本周做；P1 = 1~2 周；P2/P3 = 按节奏。

---

## 一、一句话结论

**端到端质量只卡在一件事上：多文档召回。** 其余方向（兜底路径、生成侧上线、评测口径）
都是「顺带修」，不动它；而多文档召回已有实测验证的方案，且在 **doc 级指标上**属帕累托
改进——多文档 coverage / 单文档 coverage / Hit 三项同时变好（已于 2026-09-11 上线）。

> ⚠️ **后验修正（4.7）**：P2 把覆盖率口径细化到「块」后发现，P0 的收益是「**广度换深度**」
> ——单文档问题的 top-5 从「5 块同文档」退化为「1 块 + 4 块其它文档」，chunk 级 precision
> 0.7104→0.2000。原拟用相似度阈值修正，**实测证伪**（bge-m3 上噪声/期望分布重叠）；
> 改用「锚定文档名额」旋钮后得到单调权衡曲线，**无支配点** → 取值待生成侧验证。
> 详见 4.7。

---

## 二、问题定义：一个被指标掩盖的短板

### 2.1 三个指标，测的是三种能力

| 指标 | 问的问题 | 会不会暴露「中1篇漏4篇」 |
|---|---|---|
| **Hit@5**（doc 级） | 期望文档里**至少有一篇**进 top-5 吗？ | ❌ **不会**（这就是长期没发现问题的原因） |
| **coverage@5** | top-5 命中的期望文档数 / 期望文档总数 | ✅ 会 |
| **distinct_docs@5** | top-5 里有多少篇**不同**文档 | ✅ 会（衡量同文档 chunk 抱团） |

举例：`Q-C1-00「卖家入驻该怎么做？」` 期望 `C-001~C-005` 共 5 篇。
若 top-5 全是 `C-001` 的 5 个 chunk，Hit@5 = 1.0（满分），**coverage@5 = 0.2**。
只看 Hit@5 会得出「检索没问题」的反向结论。

### 2.2 实测基线（285 问：多文档 122 / 单文档 163，生产 top_k=5）

| 指标 | 实测 | 解读 |
|---|---|---|
| 多文档 coverage@5 | **0.3755** | 该召回的文档里只有 38% 进了 top-5 |
| 多文档 distinct_docs@5 | **2.84** | top-5 平均只落在 2.84 篇不同文档上（期望平均 7 篇以上） |
| 单文档 coverage | 0.9755 | 单文档问题几乎不受影响 |
| Hit(任意一篇) | 0.9825 | **98%——所以这个短板长期不可见** |

根因：**top_k 是固定预算（5 个格子），而同一文档的 chunk 主题相近、向量相似度相近，
会「抱团占位」。** 与候选池大小无关——把候选池从 20 扩到 50 几乎无效（0.3755→0.3865），
说明瓶颈在**排序阶段的文档聚簇**，不在「候选不够」。

---

## 三、优化目标与验收标准

| 编号 | 目标 | 现状 | 目标值 | 验收方式 |
|---|---|---|---|---|
| G1 | 多文档 coverage@5 | 0.3755 | **≥ 0.50** | `coverage_by_param.py` |
| G2 | 单文档 coverage | 0.9755 | **≥ 0.99**（不得倒退） | 同上 |
| G3 | Hit(任意一篇) | 0.9825 | **≥ 0.98**（不得倒退） | 同上 |
| G4 | 生成侧多文档 coverage | 0.400 | **≥ 0.45** | `run_gen_eval.py` 重跑 |
| G5 | 全量测试 | 165 passed | 不减少 | `pytest -q` |

---

## 四、P0：多文档召回 —— 文档级去重 + 保底补位

### 4.1 算法

把「5 个格子谁分高谁占」改成「先保证每篇文档有代表，再补位」：

1. **保底**：按向量相似度降序扫描候选，**每篇文档只留第一个**（= 该文档 vsim 最高的
   那个 chunk）作为代表 → 让 top_k 尽量覆盖**不同**文档；
2. **补位**：若不同文档数**不足 top_k**（单文档/双文档问题常见），把剩余名额回填给
   **同文档的次优 chunk**（按 vsim 降序）→ 不浪费名额、不丢上下文。

实现：`backend/app/rag/retriever.py` → `diversify_with_refill()`（模块级纯函数，便于单测）。

```python
def diversify_with_refill(ranked_all, v_scores, doc_ids, top_k):
    # ① 保底：每文档取 vsim 最高的一个 chunk
    picked, picked_pos, seen_docs = [], set(), set()
    for pos, score in ranked_all:          # ranked_all 已按 vsim 降序
        did = doc_ids[pos] if pos < len(doc_ids) else f"c{pos}"
        if did in seen_docs:
            continue
        seen_docs.add(did); picked.append((pos, score)); picked_pos.add(pos)
        if len(picked) >= top_k:
            break
    # ② 补位：名额未满，回填同文档次优 chunk
    if len(picked) < top_k:
        rest = [(p, s) for p, s in ranked_all if p not in picked_pos]
        rest.sort(key=lambda x: -v_scores.get(x[0], 0.0))
        picked.extend(rest[: top_k - len(picked)])
    picked.sort(key=lambda x: -v_scores.get(x[0], 0.0))
    return picked[:top_k]
```

### 4.2 实测收益（285 问，生产 top_k=5）

| 参数组 | 多文档 coverage@5 | distinct_docs@5 | 单文档 coverage | Hit(任意一篇) |
|---|---|---|---|---|
| baseline | 0.3755 | 2.84 | 0.9755 | 0.9825 |
| rerank（当前生产） | 0.3755 | 2.84 | 0.9755 | 0.9825 |
| diverse50（硬去重，作对照） | 0.5187 | 5.00 | 0.9693 | 0.9754 |
| **diverse_refill（本方案）** | **0.5052** | **5.00** | **1.0000** | **1.0000** |
| diverse_refill20（候选池 20） | 0.5031 | 4.97 | 1.0000 | 1.0000 |

相对 baseline（=生产现状）：多文档 **+12.97pp**、单文档 **+2.45pp**、Hit **+1.75pp**
——**三项同时变好，无取舍（帕累托改进）**。G1/G2/G3 全部达标。

> 观察：候选池 20 与 50 差异仅 0.2pp（0.5031 vs 0.5052），再次印证「瓶颈不在候选池」。
> **因此落地时不必动 `QUERY_N_RESULTS=20`，改动面最小。**

**用户可见收益（生产口径，含来源阈值过滤）**：上面的 coverage@5 是「检索层」度量；
用户实际看到的是 `sources`——经 `build_hit_gate` 阈值过滤后的来源列表。按生产口径复测
285 问中的 122 个多文档问题：

| | 来源覆盖率 | 平均来源文档数 | 未命中 |
|---|---|---|---|
| 关闭 | 0.3624 | 2.29 | 1 |
| **开启** | **0.4555**（+9.31pp） | **4.28**（+1.99 篇） | **0** |

即多文档问题的来源从平均 **2.3 篇提升到 4.3 篇**。注意它低于检索层的 0.5052——
因为相似度低于 `SIMILARITY_THRESHOLD` 的补位 chunk 会被来源过滤挡掉（这是有意的：
来源要可核验，不能塞进一堆「相关度 0.4」的噪声）。

**端到端（生成侧）**：直出路径的 faithfulness / relevancy / 答案长度与开启前基本一致
（1.0 / 0.8295 / 316.5 字 vs 0.9944 / 0.8455 / 317.1 字，差异属裁判噪声）——
**因为直出只返回 top-1 原文，而 top-1 未变**。这正说明：多文档召回的收益体现在
①来源列表（上表）与 ②合成路径（LLM 能看到更多文档），而非直出路径。

### 4.3 为什么不用硬去重（diverse50）

硬去重（每文档只留 1 个 chunk、直接截断）多文档略高 1.35pp，但**单文档 coverage
从 0.9755 掉到 0.9693、Hit 从 0.9825 掉到 0.9754**——单文档问题被白白扔掉了 4 个名额。
补位把这块损失全部捞回（单文档打满 1.0000），代价只是多文档少 1.35pp。
**综合三项指标，补位严格占优。**

### 4.4 生产接入

改动点（均已就位并**已开启**）：

| 文件 | 改动 |
|---|---|
| `backend/app/rag/retriever.py` | 新增纯函数 `diversify_with_refill()`；`HybridRetriever.search()` 中 RRF 之后增加分支 |
| `backend/app/rag/config.py` | 新增开关 `DOC_DEDUP_ENABLED = False`（注释含实测依据） |
| `scripts/experiments/ragas_eval/strategies.py` | 参数组 `diverse_refill` / `diverse_refill20`，**直接 import 生产函数**（避免两份实现漂移） |
| `backend/tests/test_rag_doc_diversify.py` | 6 个纯逻辑单测 |

`search()` 中的分支：

```python
ranked = sorted(rrf.items(), key=lambda x: -x[1])[:top_k]

if DOC_DEDUP_ENABLED:
    # 必须在**全量 RRF 候选**上去重：只看已截断的 top_k 就永远救不回被挤掉的文档
    ranked_all = sorted(rrf.items(), key=lambda x: -v_scores.get(x[0], 0.0))
    ranked = diversify_with_refill(ranked_all, v_scores, self._doc_ids, top_k)
elif RERANK_BY_VSIM:
    ranked = rerank_by_vsim(ranked, v_scores, top_k)
```

**上线已完成**：`DOC_DEDUP_ENABLED=True`（2026-09-11），rerank 保持开启，两者不冲突；
缓存 key 版本同步升 `v3`；生产检索复测确认开关生效且 `results[0]` 的 vsim 与开启前完全一致。

### 4.5 影响面分析

| 关注点 | 结论 |
|---|---|
| **命中判定口径** | **不变**。输出整体按 vsim 降序，`results[0]` 仍是全局最相似 chunk，`vector_sim >= SIMILARITY_THRESHOLD` 判据原样生效 |
| **来源列表** | 更全。`build_hit_gate()` 已按 `doc_id` 去重，多文档问题会展示多个来源（原来常被同一文档占满） |
| **检索缓存** | **已处理**：缓存 key 是 `rag:search:{版本}:{语料版本}:k{top_k}:{问题}`，**不含检索策略**。上线时已把版本前缀由 `v2` 升至 **`v3`**（`cache.py::_CACHE_VERSION`），旧策略写入的结果自然不再命中 |
| **top_k 语义** | 不变（仍是 5）；补位保证单文档问题也填满 5 个 |
| **延迟/成本** | 纯排序操作，无额外 embedding / 网络调用；实测耗时与 baseline 持平（33~43s/285 问） |
| **与 rerank 的关系** | rerank 解决「正确的**那一篇**排前面」（MRR/nDCG），本方案解决「该有的**都在**」（coverage）。两者互补，不互斥 |

### 4.6 验收与回滚

**验收**：见第三节 G1~G3、G5。

```bash
# 覆盖诊断（生产 top_k=5 口径）
backend/.venv/Scripts/python scripts/experiments/ragas_eval/coverage_by_param.py \
    --params baseline,diverse_refill --out coverage_after.json

# 全量测试
cd backend && .venv/Scripts/python -m pytest -q
```

**回滚**：`DOC_DEDUP_ENABLED = False` 即恢复原行为（分支短路，rerank 继续生效）。

### 4.7 已知代价：doc 级指标看不到的「深度损失」（P2 发现 → P0-2 实施记录）

P2 把覆盖率口径从「文档」细化到「块」之后，发现 4.4 上线的保底 + 补位**并非全无代价**：

| 指标（285 问，top_k=5） | baseline（无去重） | diverse_refill20（生产） |
|---|---|---|
| doc 级 多文档 coverage@5 | 0.3755 | **0.5031** |
| doc 级 单文档 coverage | 0.9755 | **1.0000** |
| chunk 级 名额内占比（= precision@5） | **0.7032** | 0.2779 |
| └ 其中单文档问题 | **0.7104** | **0.2000** |
| chunk 级 hit1（top-1 是否为目标块） | 0.8702 | **0.9158** |
| RAGAS `context_recall`（独立实现，字符串相似度） | 0.3931 | **0.1227** |

读法：保底阶段为了「覆盖更多文档」，会把**相关度并不高的其它文档**也塞进 top-5。
单文档问题因此从「5 块同文档上下文」变成「1 块 + 4 块无关文档」
（precision 0.7104→**0.2000**）。`results[0]` 未变（hit1 反而升到 0.9158），
所以**直出路径不受影响**；受损的是**合成路径与来源列表**——这正好解释了生成侧
消融里「合成 v1 的 faithfulness 比直出低 2.6pp」。

#### 4.7.1 原修正方案（阈值判据）与**实测证伪**

原方案：给保底阶段加绝对相关性闸门——文档**首个 chunk** 的
`vsim >= DOC_MIN_SIM`（如 0.5）才占名额，否则跳过、交给补位。

**实测结论：判据在原理上不成立。** 阈值扫描（285 问，top_k=5）：

| 参数组 | 多文档 cov@5 | 单文档 cov | chunk 名额内 | chunk hit1 |
|---|---|---|---|---|
| baseline | 0.3755 | 0.9755 | 0.7053 | 0.8702 |
| diverse_refill20（无闸门） | 0.5031 | 1.0000 | 0.2779 | 0.9158 |
| + 闸门 0.3 / 0.4 / 0.5 | 0.5031 | 1.0000 | 0.2779 | 0.9158 |
| + 闸门 0.6 | 0.4954 | 1.0000 | 0.3200 | 0.9158 |

`0.3/0.4/0.5` 三档与「无闸门」**四项指标完全一致**——闸门根本没触发。判据诊断
（单文档 163 问 / 多文档 122 问）给出根因：

| 代表块 vsim / best 比值 | 中位 | 说明 |
|---|---|---|
| 单文档问题的**非期望**文档 | **0.898** | 「相关但非期望」 |
| 多文档问题的**期望**文档 | **0.934** | 「期望」 |

两个分布**严重重叠**。bge-m3 的余弦相似度被压缩在窄区间（候选池内各文档代表块
普遍 ≥0.55），「沾边但非目标」与「目标」文档在 vsim 标尺上几乎同分。绝对阈值
0.65 的噪声保留率 0.690 / 期望保留率 0.735，**几乎无区分度**；抬到 0.70 才开始
分开，但期望文档已同步被砍掉近半。结论：**不要用 vsim 单点去猜问题类型**。

#### 4.7.2 修正方案（锚定文档名额）与实测曲线

根因重新定位：问题不在「进了低相关文档」，而在**top1 所在文档只保了 1 个块**——
单文档问题 top-5 里「期望文档」占位均值 **恰好 1.00**（baseline 3.56）。而
「top1 文档是主文档」这个假设**可靠**（hit1 0.9158）。故改为显式旋钮
`ANCHOR_QUOTA`（`diversify_with_refill(..., anchor_quota=k)`）：允许 top1 所在文档
在保底阶段多占 k 个名额，其余文档仍各 1 个；补位逻辑不变。

| 参数组 | 多文档 cov@5 | distinct | 单文档 cov | Hit | chunk 名额内 | └ 单文档 | chunk hit1 |
|---|---|---|---|---|---|---|---|
| baseline | 0.3755 | 2.84 | 0.9755 | 0.9825 | 0.7053 | 0.7129 | 0.8702 |
| 配额 1〔现线上〕 | **0.5031** | 4.97 | **1.0000** | **1.0000** | 0.2779 | 0.2000 | 0.9158 |
| 配额 2 | 0.4622 | 4.20 | 0.9939 | 0.9965 | 0.4246 | 0.3742 | 0.9158 |
| 配额 3 | 0.4372 | 3.63 | 0.9939 | 0.9965 | 0.5551 | 0.5325 | 0.9158 |
| 配额 4 | 0.4046 | 3.20 | 0.9939 | 0.9930 | 0.6505 | 0.6528 | 0.9158 |

**三条读法**：

1. **曲线单调、机制成立**：配额 ↑ → 深度 ↑（chunk 名额内 0.2779→0.6505）、广度 ↓
   （多文档 cov 0.5031→0.4046）。取代了「阈值无效」的失效状态，把权衡变成可调旋钮。
2. **配额 2/3/4 全面优于 baseline**（除 chunk 精度）：多文档 coverage +2.91~+8.67pp、
   单文档 coverage +1.84pp、Hit +1.05~+1.40pp、**单文档 hit1 +7.36pp**（0.8405→0.9141）；
   只有 chunk 名额内略低于 baseline（配额 4 已达 0.6505 vs 0.7129，恢复 91%）。
3. **不存在支配点**：没有一组能同时最大化广度与深度——受 top_k=5 固定预算约束，
   「广度换深度」是硬约束、不是实现缺陷。上线取值需要**生成侧（答案质量）证据**，
   因为 precision 的代价（LLM 看到 4 块非目标上下文）与收益（多文档信息覆盖）
   都只在下游体现。

**当前状态**：机制与单测已落地，`ANCHOR_QUOTA` 默认 **1**（不改变现网行为）；
配额 2/3/4 的取值待生成侧验证后确定。复现：
`coverage_by_param.py --params baseline,diverse_refill20,aq2,aq3,aq4 --top-k 5`。

> 附注：chunk 级 precision 的「损失」有部分是**标注口径造成的**——golden 对单文档
> 问题只标 1 篇期望文档，但语料里存在大量主题相近的姊妹文档，检索到它们未必无用。
> 故 4.7.2 的取舍最终只能由生成侧答案质量裁定，不能只看检索指标。

---

## 五、P1：兜底路径注入检索上下文

> **状态：✅ 已实现（2026-09-11）**。三档分流已落地，实现清单见 5.3、验收见 5.4。

### 5.1 现状与风险

`rag_service._llm_fallback()` 目前**完全不携带检索结果**——只用 system 提示词
（「知识库没有就说没有，不要编造费率/时限/制度条款」）+ 历史 + 问题，让模型**凭参数知识**作答。

两个变化让这条路径的风险放大：

1. 命中阈值已从 0.55 上调到 **0.65**（commit `940743c`）→ **更多问题会走到兜底路径**；
2. 兜底答案没有来源可核验，用户无法判断它是从知识库来的还是模型「想」出来的。

### 5.2 方案（两档，建议都做）

1. **注入「临界片段」**：把相似度落在 `[0.4, SIMILARITY_THRESHOLD)` 的 top-k 片段
   带**标注相似度**注入 prompt，明确告诉模型「以下是与问题可能相关的知识库片段，
   相似度不高，请仅作参考、不要编造」；
2. **明确拒答**：若连 0.4 都不到，直接回「知识库暂无相关内容」，**不调用 LLM**
   （省成本，也杜绝编造）。

### 5.3 实现清单

三档分流（原为「未命中 → 一律调 LLM」两档）：

| 档位 | 条件（`results[0].vector_sim`） | 行为 |
|---|---|---|
| **命中直出** | `≥ SIMILARITY_THRESHOLD`（0.65） | 直出原文 + 来源（原行为，未变） |
| **注入兜底** | `[FALLBACK_CONTEXT_MIN_SIM, 0.65)` | 注入临界片段 → 调 LLM → 返回答案 + 片段来源 |
| **直接拒答** | `< FALLBACK_CONTEXT_MIN_SIM`（0.4）或结果为空 | 回「知识库暂无相关内容」，**不调 LLM** |

改动点：

- `backend/app/rag/config.py`：新增 `FALLBACK_CONTEXT_MIN_SIM = 0.4`、
  `FALLBACK_CONTEXT_MAX_CHUNKS = 3`（含取值依据注释）；
- `backend/app/services/rag_service.py`：
  - 新增 `build_fallback_context(results)` → 提取灰区 `[0.4, 0.65)` 片段（按 `doc_id` 去重、
    上限 3 条），低于下限返回 `None`；
  - 新增 `format_fallback_context(docs)` → 序列化为带「来源 + 相似度」标注的注入文本；
  - `_build_fallback_messages()` 新增 `retrieved_context` 参数（与 `competitors_context` 同构）；
  - `_llm_fallback()` / `_llm_fallback_stream()` 新增 `retrieved_context` 透传；
  - `ask()` 接入三档分流，兜底时把临界片段作为 `sources` 返回（**前端 sources 渲染与 mode 无关，
    无需改前端**）；
  - 顺带修正：成本预估原用 `_FALLBACK_SYSTEM_PROMPT` 常量而非实际 system 内容，
    注入片段后会低估 token → 改为按完整 `messages` 估算。
- `backend/app/api/rag.py`：流式端点 `ask_stream()` 接入同样三档（拒答 / 注入 / 命中）。

### 5.4 验收

**单测（已全绿）**：`backend/tests/test_rag.py` 新增/更新 7 个用例、`test_rag_sessions.py` 2 个：

- `test_ask_gray_zone_injects_context_and_returns_sources`：灰区 → 注入片段 + 返回来源；
- `test_ask_rejects_without_llm_when_all_chunks_too_weak`：`top1 < 0.4` → 拒答且**断言不调用 LLM**；
- `test_ask_empty_results_rejects_without_llm`：无结果 → 拒答且不调用 LLM；
- `test_ask_stream_rejects_without_llm_when_too_weak`：流式路径同样的拒答断言；
- `test_build_fallback_context_*`：灰区筛选 / 命中区排除 / `doc_id` 去重 / 条数上限；
- `test_format_fallback_context_annotates_doc_and_score`：注入文本带来源与相似度标注。

**误伤面（实时探针实测，`fallback_gate_probe.py`，耗时 43.9s）**：

| 集合 | 条数 | 命中直出 | 注入兜底 | 拒答 |
|---|---|---|---|---|
| 领域内（golden 285 问） | 285 | **285** | **0** | **0** |
| 领域外（人工构造） | 15 | **0** | 12 | 3 |

- **领域内零误伤**：285 问全部落在命中直出档，无一被注入档或拒答档截走。
- **领域外零漏挡**：15 条闲聊/通用问题（天气、写诗、减肥、1+1、股市…）**没有一条**
  进入命中直出档；其中 12 条落 `[0.4, 0.65)` 注入档（会带标注片段再让 LLM 作答，
  最差情况也有「来源 + 相似度」可核验），3 条（写诗 / 科幻小说推荐 / 特朗普是谁）
  落 `< 0.4` 拒答档。
- 与逻辑论证一致：下限 0.4 远低于领域内最低 top1（正确样本 0.6660 / 错误样本 0.6483），
  领域外最高仅 0.5427（iPhone vs 安卓）——**两档之间存在安全间隔**。

原始数据：`scripts/experiments/ragas_eval/results/fallback_gate_probe.json`。
复现：`backend/.venv/Scripts/python scripts/experiments/ragas_eval/fallback_gate_probe.py`。

**上线后建议监控**：`mode=fallback` 占比与「拒答」占比。若拒答率过高（如 > 5%），
说明下限 0.4 仍偏高，应下调或改为「注入但不拒答」。

---

## 六、P1：合成答案按问题类型分流上线

### 6.1 消融结论回顾（40 问分层抽样，DeepSeek 通道，实花 ¥1.36）

| 指标 | 直出原文 | 合成 v1（内联来源） | 合成 v2（末尾集中） |
|---|---|---|---|
| faithfulness | 0.9944 | 0.9682 | 0.9439 |
| answer_relevancy | 0.8455 | 0.8986 | 0.9084 |
| 来源覆盖度 | 0.6025 | 0.7000 | 0.7000 |
| 平均耗时 | 0s | 2.02s | 1.97s |

- **v1 优于 v2**：v1 要求边写边挂来源，模型全程被约束 → 忠实度更高；v2 行文自由但更易漂移。
- **直出 faithfulness≈1 是天花板效应**（答案就是片段原文），不能读成「合成在编造」。

### 6.2 分流策略

| 问题类型 | 策略 | 理由 |
|---|---|---|
| 单文档（coverage 已达 1.00） | **直出原文** | 合成收益仅 +1.9pp，却白付 2 秒延迟 |
| 多文档 | **v1 合成** | 覆盖度 +9.8pp、相关性 +5.3pp，是真实收益所在 |

并加「**来源可点验**」：合成答案每条来源仍挂 `doc_id`，前端可点击查看原文。

### 6.3 成本与延迟

- 每问 +2s、+¥0.0118（低谷价，含裁判成本，实际线上可省去裁判）。
- 复用已有成本熔断 + 检索缓存；对同问同片段走**答案缓存**（建议 key 加「答案风格」维度）。

### 6.4 验收

- 生成侧重跑：多文档 coverage ≥ 0.45，`faithfulness` 无 `<0.6` 样本（不出现编造）；
- 灰度：先只对多文档问题开，观察 👍/👎 反馈与 `mode` 分布。

---

## 七、P2：评测基建

| 项 | 状态 | 说明 |
|---|---|---|
| **coverage@5 固化为一等指标** | **已完成（2026-09-11）** | 口径抽到 `metrics.py`（`coverage_at_k` / `aggregate`，`PRODUCTION_TOP_K=5`），`run_eval.py` 每次输出都打印「多文档 coverage@5 / 单文档 coverage@5 / Hit(任意@5)」，JSON 里新增 `coverage.at5` 与 `coverage.at_k`（逐问也有 `coverage_at5` / `distinct_docs_at5`）；`coverage_by_param.py` 改为复用同一实现（**消灭两处口径漂移**，那正是曾导致反向结论的根因）；`compare.py` 对比表新增覆盖率段（旧 JSON 无字段时自动跳过） |
| **chunk 级 golden** | **已完成（2026-09-11）** | 不引入人工标注，而是把**期望块集合**定义为「期望文档下的全部 chunk」——由 `common.doc_chunk_ids()` 从 `all_chunks()` 枚举 `c<pos>` 复现，与 `bm25_index.json` 的 `doc_ids` **逐位一致**（7619 块 / 1000 篇；测试守卫 `test_doc_chunk_ids_与bm25索引逐位对拍`）。新增 `metrics.chunk_coverage_at_k`（`recall` / `recall_norm` / `precision` / `hit1`）与 `aggregate_chunks`（多/单文档 + 期望块数分箱），`run_eval.py` 与 `coverage_by_param.py` 同步输出，并新增 `chunk_id` 字段（`retriever.search`）。**据此发现 P0 的隐藏代价，见 4.7** |
| **指标选择纪律** | 已固化 | 评价检索策略**必须同时**看 Hit@5 与 coverage@5——只看 Hit@5 曾把 `diverse` 误判为「无效」，得出反向结论（见 `RAGAS 评估实验.md` 第九节）。现已写进 `run_eval.py` 模块文档与单测守卫（`test_run_eval_输出覆盖率一等指标` 断言 coverage 不得从输出里消失） |

---

## 八、P3：语料 / 分块观察项

- **FAQ 按条分块（24 条 / 文档）导致 chunk 极碎**：同一 FAQ 文档会有 20+ 个 chunk
  争抢 top-5 名额，与「文档聚簇」问题同源。可评估：FAQ 整篇合成一个 chunk、
  或对 FAQ 类文档单独设 `max_chunks_per_doc`。
- **golden 与语料同源偏差**：285 问与语料出自同一生成流程，绝对分偏乐观，
  横向比较（A/B）可靠、绝对值仅作参考。

---

## 九、实施顺序

1. **P0（✅ 已完成 2026-09-11）**：开 `DOC_DEDUP_ENABLED=True` + 缓存 key 版本升 `v3`
   → 检索侧复测达标（多文档 0.5052 / 单文档 1.0000 / Hit 1.0000）→ 生成侧端到端复跑验证。
2. **P1（✅ 已完成 2026-09-11）**：兜底路径三档分流（拒答 / 注入临界片段 / 命中直出），
   含 `build_fallback_context` + `retrieved_context` 注入 + 流式端点同步；单测全绿。
   实时探针待向量库稳定后补跑（见 5.4）。
3. **下一步（P1）**：合成 v1 按问题类型分流灰度。
4. **P2（✅ 第一、二项已完成 2026-09-11）**：coverage@5 固化为 `run_eval.py` 一等指标
   （`cfef9e0`）；chunk 级 golden / 覆盖率落地（期望块集合由 `common.doc_chunk_ids()`
   从 `all_chunks()` 复现，与 bm25 索引逐位对拍）→ **据此暴露 P0 的深度损失，见 4.7**。
5. **P0-2（✅ 机制已落地 2026-09-11，取值待定）**：原「相关性闸门 `DOC_MIN_SIM`」判据
   经阈值扫描**实测证伪**（bge-m3 上噪声/期望 vsim 分布重叠，0.3~0.5 不触发）；
   改为**锚定文档名额** `ANCHOR_QUOTA`（top1 文档可多占名额），曲线单调、配额 2/3/4
   在 doc 级指标与 hit1 上全面优于 baseline，但无支配点。默认配额 1（不改变现网行为），
   取值待**生成侧答案质量**验证后确定。详见 4.7。
6. **随后（P1-② / P2 剩余 / P3）**：合成答案按问题类型分流灰度、FAQ 分块观察。

---

## 十、附：复现命令

```bash
cd <项目根目录>

# 多文档覆盖诊断（生产 top_k=5）
backend/.venv/Scripts/python scripts/experiments/ragas_eval/coverage_by_param.py \
    --params baseline,rerank,diverse50,diverse_refill,diverse_refill20

# 检索侧 A/B
backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_eval.py \
    --params baseline --strategy baseline --out baseline.json

# 生成侧（需在 backend/.env 配好 LLM_API_BASE / LLM_API_KEY / LLM_MODEL）
backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_gen_eval.py \
    --sample 40 --skip-synth
backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_gen_eval.py \
    --sample 40 --synth-styles v1,v2 --skip-direct --resume --budget-cny 2.0
backend/.venv/Scripts/python scripts/experiments/ragas_eval/compare_gen.py
```

---

## 与既有文档的关系

| 文档 | 关系 |
|---|---|
| `scripts/experiments/ragas_eval/RAGAS 评估实验.md` | **实测记录**（第一~九节）：本方案的每个数字都出自那里 |
| `docs/现状分析与优化方向.md` | 仓库级体检报告。其 **F4「多文档召回」原标 P1，并把短板写成「单文档 coverage」**——经本次实测应**升级为 P0**，且短板在**多文档**（单文档 coverage 已达 0.9755→1.0000） |
| `docs/RAG技术说明.md` | 架构说明。本次改动若上线，其第 3 节「混合检索」需补一句文档去重 |
