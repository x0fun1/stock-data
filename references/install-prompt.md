# Skill 安装 Prompt

将下面整段交给能操作本地终端的安装助手。可以在开头补充安装目标目录和已有仓库路径。

```text
请安装或更新 stock-research Skill，源码仓库为 https://github.com/x0fun1/stock-data，使用 main 分支。除了 Skill 文件，请一并部署它需要的本地 Search / Fetch 服务，完成后提供可运行的命令与验证结果。

1. 使用 Python 3.12 或以上版本。已有源码目录时先检查分支和未提交改动，保留本地改动；新安装则克隆 main。阅读仓库 SKILL.md 与 references/local-search-fetch.md，并选择用户指定或当前宿主使用的 Skill 安装目录。
2. 从源码目录执行 python scripts/install_skill.py --destination INSTALL-DIR；目标已经是 stock-research 时加 --update。安装器会把源码和文档复制到目标，在目标内创建独立 .venv，并从 requirements.txt 安装 requests、ddgs>=9,<10、trafilatura>=2,<3。正常安装不要加 --skip-deps 或 --skip-smoke。安装依赖与运行命令使用 Python 3.12+，不要修改全局 Python 包。
3. 部署的本地应用有两个逻辑接口：Search 使用 DDGS 搜索；Fetch 使用 requests 下载公开 HTML、Trafilatura 抽取正文。两者由 Skill 自带的 news-local-serve 进程一起提供，不需要 Docker、TinyFish 连接或 TinyFish API Key。默认监听 127.0.0.1:8765，提供 POST /search、POST /fetch 和 GET /health。
4. 在安装后的目录启动 python scripts/skill.py news-local-serve，并用 GET http://127.0.0.1:8765/health 检查服务。标题采集、服务搜索和抓取默认并发均为 32，不设置固定搜索间隔。若端口已被占用，先识别已有服务；兼容且默认并发为 32 时复用，否则选择空闲端口并为采集命令同时设置 --search-url 和 --fetch-url，不结束不相关进程。用当前宿主支持的方式保持进程运行，记录启动命令、进程或日志位置；不要擅自配置系统开机启动。如果宿主不能保持后台进程，明确提供独立终端启动命令。
5. 如果公网域名被本机 Tun 代理解析为 198.18.0.0/15 的 Fake-IP 地址，确认该网络模式后，在启动命令加 --allow-vpn-fake-ip；普通网络不要加。需要 SearXNG 时才使用 --search-backend searxng --searxng-url LOCAL-URL，并确认其 JSON 搜索已启用；SearXNG 是可选后端，不是默认安装依赖。
6. 核对安装器自检包含报告生成、DDGS 导入、Trafilatura 实际抽取，以及 search_workers=32、fetch_workers=32。/health 成功只能证明本地服务就绪；还需通过本地 /search 搜索一条公开公司公告，再调用 /fetch 读取结果中的公开文章，记录网络成功或具体失败，不把本地存档当作联网验证，不将取得正文当作事实已核实。
7. Finnhub 和 FRED 凭证使用用户已有的本地配置或环境，不把凭证写进命令参数、安装 Prompt、Git 或共享文档。保留运行数据和旧证据；首次没有新闻数据时说明如何导入标题或使用包内 FinnhubClient，随后准备 workflow，再用 news-collect 采集。

交付时报告 Skill 安装目录、Python 版本、依赖与自检结果、本地服务地址和运行方式，以及以下后续命令（替换目录、workflow ID 和 collection ID）：
  python scripts/skill.py news-workflow --root MY-DATA --ticker NVDA --workflow-id NEWS-ID --step prepare
  python scripts/skill.py news-collect --root MY-DATA --workflow-id NEWS-ID --collection-id LOCAL-ID
news-collect 默认使用本地 provider 和 32 并发；可显式用 --workers 16 调低并发。上游限流、付费墙、JavaScript 页面与未知发布时间保留为访问缺口。
```
