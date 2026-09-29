# 开盘信号与模拟成交复盘（未部署示例包）

**仅供独立隔离环境代码审阅和改造；不是可直接部署或运行的完整系统。** 本包不包含私人服务 ID、真实调度清单、凭证、真实数据或消息目标；未安装、启用或修改任何本机调度，也未发送消息。目录中的脚本/launchd 模板仅是示例，不表示原系统的运行状态或执行时间。**不要直接加载 plist 或接入生产调度。**

## 文件说明

- `scripts/run_d1_opening_signal_feishu.sh`：本地开盘信号入口；以当日锁和成功标记防重；文件名只是历史示例名，默认不发送消息。
- `scripts/run_d1_opening_signal_backtest_style.py`：信号入口；需外部策略模块及同日竞价数据。`REVIEW_SEND_HOOK` 默认为空；只有用户自行实现并明确指定可执行 hook，才把信号路径传 argv[1]、文本传 stdin。hook 返回 0 仅表示 hook 执行成功，不保证消息送达。请先审计发送对象和防重行为。
- `scripts/run_opening_signal_fill_review.sh`、`scripts/run_d1_fill_review_launchd.sh`：两种本地复盘入口；后者要求同日 stocks parquet，`REVIEW_WAREHOUSE_MODE=update` 还需要未打包下载器。均不发送消息、不读取个人 shell profile。
- `scripts/opening_signal_fill_review.py`：按显式目标日期运行的模拟复盘；必须有目标日且 JSON 内声明同日的信号，以及可信的同日开盘、收盘价。缺失或不可确认的收盘数据直接拒绝，不拿代理价格计算盈亏，也不保存账本。
- `scripts/local_stock_warehouse.py`：本地仓库工具；update 所需 `scripts/market_data_warehouse_downloader.py` 未打包。
- `launchd/com.example.d1-fill-review.plist.template`：无自动执行时刻的示例模板；占位符必须由用户在隔离环境配置、验证，不能直接加载。

## 外部依赖与安全边界

这是**不完整**的代码样例：`opening_signal_fill_review.py` 依赖 `REVIEW_WORKDIR` 中未打包的 `scripts.rolling_backtest_baseline_v1`、`stock_analyzer.core.ifind_api` 及其下游模块、合法授权和本地 parquet。开盘信号入口还依赖未打包的策略生成模块、前一交易日 stocks parquet、目标日竞价 parquet（至少 50 行）及可选的同日 Fuyao 快照。默认 Fuyao 路径要求本地竞价文件；本包没有 MX 构建器。Python 依赖含 pandas、parquet engine、requests、python-dotenv 等，未锁定版本。不得复制本机 `.env`、个人配置、密钥、真实账本或真实收件人。

目标日期必须为 `YYYY-MM-DD`；信号文件必须在 JSON 顶层声明与目标日期一致的 `trade_date` 或 `date`，若两字段并存须都一致。找不到当日信号时不会改用其他日期；显式 `--signal`、审计路径和标记路径也需通过内容日期校验。股票本地快照需有可解析、归属上海时区目标日 15:00 之后的 `fetched_at`；否则仅尝试有日期校验的外部 EOD 查询，不可确认时失败关闭。运行前自行验证股票代码、数据完整性、交易日、时区及模拟口径。

在**隔离环境、备齐并审计全部依赖与样例数据后**，可用以下命令试运行（将路径替换为测试目录；会写模拟账本，先备份）：

```sh
export REVIEW_WORKDIR=/path/to/review-test-data-and-external-modules
export REVIEW_PACKAGE_DIR=/path/to/review-tools-export
export REVIEW_PYTHON=/path/to/python3
export REVIEW_WAREHOUSE_MODE=require
"$REVIEW_PYTHON" "$REVIEW_PACKAGE_DIR/scripts/opening_signal_fill_review.py" --date YYYY-MM-DD --signal /path/to/same-day-signal.json
```

仓库与报表数据不应加入分发包。信号中缺少/不一致的日期、无法证明同日收盘的行情均应先修复数据而非跳过校验；静态代码检查不能证明外部依赖及真实行情的正确性。本公开目录仅含脱敏示例；原始生产脚本、数据、授权配置及个人消息接收人未公开。
