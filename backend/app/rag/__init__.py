"""RAG 检索增强模块：基于二手交易知识库语料的问答检索能力。

复用 RAGreflow-second-hand 已验证的混合检索链路（bge-m3 + Chroma + BM25 + RRF），
不引入 langchain（B 项目虽安装但检索未使用，冗余依赖）。
"""
