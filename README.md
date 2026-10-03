# stock-data

统一美股、港股股票数据 skill：美股在 Finnhub MCP 能力范围内优先使用 Finnhub，失败或覆盖不足时按数据项回退到 global-stock-data。港股及 Finnhub 未覆盖能力直接使用 global。

## 目录

```text
stock-data/
  SKILL.md                              唯一技能入口
  scripts/global_stock_data.py          可导入的回退实现与 JSON 命令入口
  references/finnhub-mcp-1.21.3-schema.md Finnhub 参数与字段基线
  references/global-stock-data.md        函数、来源顺序及数据口径
  references/source-policies.md          来源限制与环境配置
source-material/                        迁移前资料存档，不作为运行指令
tests/                                 离线行为测试
```

安装时使用整个 `stock-data/` 目录。Finnhub MCP 连接、API key 和授权由使用者已有环境提供；Python 回退需要 Python 3.10+、requests 与允许访问来源的网络。SEC 功能从环境变量读取 `SEC_CONTACT`，CBOE 仅在已有授权并配置 `CBOE_AUTHORIZED=1` 后启用。

在仓库根目录检查回退函数和执行离线测试：

```text
python stock-data/scripts/global_stock_data.py --list
python -m unittest discover -s tests -v
```

没有进行本次实时行情或网站条款核验；运行时以当前 MCP schema、实际响应及适用来源条件为准。市场覆盖为美股和港股，其他市场没有完整路由。

存档文件保留迁移前文字及历史测试记录，其中旧调用顺序和来源结论可能与当前实现不同。global-stock-data 原作者为 Simon Lin，原项目为 https://github.com/simonlin1212/global-stock-data；原 Finnhub 资料署名为 xo / Hermes Agent；原资料中的许可声明只对应其原文件，本次不推定整个组合包的许可。
