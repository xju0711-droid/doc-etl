# 小菊抽屉 · doc-etl

> 把乱糟糟的 PDF / 网页 / 文本,塞进抽屉,出来是分门别类的结构化数据。

一个基于 LLM 的文档智能抽取工具。告诉它"我要从这些文档里提取什么字段",它用大模型读完每份文档,返回干净的 JSON / Excel。

## 核心特性

- **多模型降级链** — 主模型(Claude)→ 备用(GPT-4o-mini / 本地 Ollama)。按错误类型区分可重试(429/超时/5xx)与不可重试(401 鉴权失败),避免无效重试浪费配额。
- **JSON 修复状态机** — LLM 输出格式异常时,三层递进修复:正则提取 → LLM 回喂修复 → 宽松解析。
- **Token 成本全链路追踪** — 覆盖修复调用,精确到每次请求的 USD 成本。
- **优雅关闭 + 批次稳定判断** — Ctrl+C 不丢失已完成数据;完成率 ≥ 95% + 静默期判定批次稳定,适配生产场景。
- **双哈希缓存** — 文档内容 + Schema 定义共同决定缓存键,避免字段变更时复用旧结果。
- **动态 Schema** — 命令行传字段描述,合同/简历/发票/论文通用,不绑死场景。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
cp .env.example .env
# 编辑 .env,填入 ANTHROPIC_API_KEY 或 OPENAI_API_KEY
# 或使用本地 Ollama,无需 API Key

# 3. 运行
python main.py run report.pdf -f title=标题:required -f summary=摘要
```

## 命令示例

```bash
# 单文档
python main.py run 合同.pdf -f party_a=甲方:required -f party_b=乙方:required -f amount=金额

# 批量处理(支持 Ctrl+C 优雅中断)
python main.py batch ./docs --ext pdf -f title=标题 -f date=日期

# 处理网页
python main.py run https://example.com/article -f title=标题 -f summary=摘要
```

字段格式:`-f 名称=描述[:required][:list|str|int|float]`

## 项目结构
doc-etl/
├── parsers/        # 解析层:PDF (PyMuPDF) / 网页 (trafilatura) / 文本
├── extractor/      # 抽取层:Prompt 构造 + LLM 降级 + JSON 修复状态机
├── cache/          # 缓存层:双哈希键 + diskcache
├── queue_worker/   # 队列层:ThreadPoolExecutor 异步并发
├── evaluator/      # 评估层:规则打分 + 质量报告
├── exporter/       # 导出层:Excel 自动分 Sheet 追加
└── main.py         # CLI 入口

## 关键设计决策

### 为什么用 ThreadPoolExecutor,不用 Celery?

Celery 需要单独跑 Redis broker、配 worker 进程,对单机场景太重。ThreadPoolExecutor 是标准库,零额外依赖,LLM 调用是 IO 密集型(等网络),线程池完全够用。如果未来需要跨机器分布式,只需要替换 `queue_worker/worker.py`,其他层零改动。

### 为什么缓存键要哈希 Schema?

如果只哈希文档内容,会出现 bug:第一次只抽 `title`,缓存命中;第二次想多抽 `author`,但文档没变,缓存返回旧结果——`author` 永远是 null。所以缓存键必须包含"这次想抽什么"的描述。

### 为什么质量评估用规则,不用 LLM-as-Judge?

LLM-as-Judge 适合判断语义质量(摘要写得好不好)。但当前最常见的失败模式是结构性的:JSON 解析失败、必填字段为 null、填充率过低——这些用规则毫秒级搞定,零额外成本。LLM-as-Judge 留作后续扩展。

### 降级链里"什么错误算可重试"?

只把 429(限流)/ 5xx(服务端故障)/ 超时 / 网络断开算可重试。401 鉴权失败、400 请求格式错误是配置问题,重试一万次也是同样的错,反而浪费配额。

## 可改进方向

- **语义缓存**:目前是精确缓存。可用 embedding + 向量相似度做语义命中。
- **LLM-as-Judge 评估**:对语义质量做二次评分。
- **跨文档审计 (Reviewer)**:批量结果中检测缺漏字段、格式异常、重复实体。
- **可观测性**:Prometheus 指标导出 + trace_id 全链路追踪。

## 技术栈

Python 3.10+ / Pydantic / diskcache / Anthropic & OpenAI SDK / PyMuPDF / trafilatura / Click / Rich / openpyxl

## License

MIT