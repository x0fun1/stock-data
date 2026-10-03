# Finnhub 与 global-stock-data 统一技能包

## 唯一入口

- `stock-data/SKILL.md`：统一股票数据 skill。美股在 Finnhub 能力范围内优先调用 Finnhub MCP；调用失败或缺少必需字段时，逐项回退到 global-stock-data。港股和 Finnhub 未覆盖的能力走 global-stock-data。

## 参考材料

- `stock-data/references/finnhub-mcp-1.21.3-schema.md`：Finnhub MCP 参数、字段、单位及错误 envelope 基线；运行时以当前工具 schema 为准。
- `stock-data/references/global-stock-data.md`：global-stock-data 原始数据层、代码、helper 和来源限制。使用 SEC 功能前，须把示例 SEC_CONTACT 改为使用者自己的真实联系信息。
- `stock-data/references/source-material/finnhub-stock-analysis.md`：迁移前 Finnhub skill 原文，仅供追溯，不是独立入口。

Finnhub MCP 配置和 API key 不包含在此包中。Finnhub 只能通过当前环境暴露的 MCP 工具访问；本包不包含行情数据。
