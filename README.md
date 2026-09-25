# 前缀索引与候选排序（从 0 实现）

给一份词表建索引落盘，之后按前缀取回前 K 个候选，并给出可逐条复核对齐的分数。环境里只有说明与素材，实现要新写。

交付形态：仓库根目录的 `cli.py`（命令行入口）与 `web/index.html`（原生 ES module 页面）。只用 Python 3.13 标准库和浏览器原生能力，无第三方依赖、无构建步骤、不引用 CDN。

## 一、范围

要做：词表解析、索引构建与落盘、前缀查询与排序、命令行与 HTTP 接口、静态页面。

明确不做：

- 不做模糊匹配、拼音、纠错、同义词、分词；
- 不做大小写折叠、全半角折叠、Unicode 归一化；
- 不做增量更新、删除、多词表合并、索引格式迁移；
- 不做权限、限流、多进程或多机一致性；
- 页面样式、键盘导航、防抖策略、高亮方式不属验收范围。

## 二、口径与公式

词条为 `(词, 频率, 权重)`，权重可省略，省略时按 0 计。`L` 为词的 Unicode 码点数（组合字符按多个码点计，emoji 按 1 个码点计）。

分数用整数运算定义：`S100 = 频率 × (100 + 权重) × (100 + L)`，展示值 `S = S100 / 100`，固定两位小数（如 `13230.00`、`4573.20`），不得用浮点四舍五入。

匹配是精确前缀匹配，按码点逐位比较：大小写不同、全半角不同、合成与分解形式不同都算不同的词。

排序是全序，依次比较：`S100` 降序 → 频率降序 → `L` 升序 → 词按 Unicode 码点升序。四轮比完必分先后，同一查询的结果唯一。

## 三、数据结构与落盘

- 索引要支持「按前缀定位候选区间」与「区间内取前 K」，定位代价为 O(log n + m)（n 为词条数，m 为命中数）；查询时不得遍历整份词表。
- 空前缀等价于「全局前 K」（清空搜索框时给默认推荐），必须由构建期算好的排名直接给出，不能临时全表扫描。
- 索引目录结构自定，但要含 `manifest.json`：UTF-8 JSON，至少有 `version`（整数，当前为 1）、`entry_count`（有效词条数）、`source_sha256`（词表文件字节的 SHA-256 十六进制小写），允许附加字段。
- 构建要么完整成功要么不留痕迹：先写临时位置，成功后再替换；`manifest.json` 存在即代表索引可用。
- 查询阶段只读索引，不再读词表；索引目录可整体搬移，不得依赖绝对路径。
- 同一词表重复构建，查询结果必须一致（不要求索引字节一致）。

## 四、输入输出与文件格式

### 词表 `samples/words/*.txt`

- UTF-8（开头的 BOM 忽略），LF 或 CRLF 行尾均可。逐行处理：先去掉行尾空白；去尾后为空的行忽略；去掉行首空白后以 `#` 开头的行忽略；其余行按 TAB 切分。
- 字段为 `词 <TAB> 频率 [<TAB> 权重]`，字段数只能是 2 或 3。
- 词：非空、不含任何空白字符（按 `str.isspace()` 判定）、码点数 ≤ 4096。
- 频率：纯十进制数字，`1 ≤ 频率 ≤ 10^9`；权重：纯十进制数字，`0 ≤ 权重 ≤ 10^5`。
- 同一词重复出现时按首次出现的那行计，后续重复行忽略。
- 空词表合法（`entry_count = 0`）。非法行必须让整份构建失败：退出码 1，指出文件与行号，且不产出索引。

### 查询清单 `samples/queries/*.txt`

按同样的规则切分成 `前缀 <TAB> K`：前缀可为空串（该行以 TAB 开头），不含空白字符，码点数 ≤ 4096；`K` 为十进制整数，`0 ≤ K ≤ 1000`。清单先整体校验，再执行查询；任何非法行都不得产生已输出的结果。

### 结果输出（stdout）

每条查询按排名升序给出候选，每行五个 TAB 分隔字段：

`前缀 <TAB> K <TAB> 序号 <TAB> 词 <TAB> 分数`

序号从 1 开始；无候选时只输出 `前缀 <TAB> K <TAB> 0 <TAB> - <TAB> -` 一行。多条查询按清单顺序拼接。编码固定 UTF-8（无 BOM）、LF 行尾、末尾有换行，不受系统默认代码页影响。

### 命令

- `python cli.py build --words <词表> --index <索引目录>`
- `python cli.py query --index <索引目录> --queries <查询清单>`
- `python cli.py bench --index <索引目录> --queries <查询清单> --repeat <N>`：stdout 一行 JSON，含 `queries`、`repeat`、`p50_ms`、`p95_ms`、`p99_ms`、`rss_mb`。
- `python cli.py serve --index <索引目录> --port <端口>`：同时托管 `web/` 与接口。

索引目录建议放在 `var/` 下（已在 `.gitignore` 中）。

退出码：`0` 成功；`1` 输入非法（词表或查询清单）；`2` 用法错误或索引不可用、版本不匹配。

### HTTP 接口

`GET /api/complete?prefix=<前缀>&k=<K>` 成功时返回 `200` 与 `application/json; charset=utf-8`，正文形如 `{"k":10,"candidates":[{"word":"apple","score":"13230.00"}]}`；候选顺序与分数文本和命令行完全一致。`k` 缺省 10，范围 `0 ≤ k ≤ 1000`；`prefix` 允许为空。参数非法返回 `400` 与 `{"error":"…"}`。

## 五、性能与验收口径

以 `samples/words/big.txt`（100000 条，约 1.7 MB）与 `samples/queries/big.txt`（12 条查询）为准，阈值：构建 ≤ 8 s；构建与查询峰值内存 ≤ 512 MB；索引体积 ≤ 词表文件字节数的 3 倍；冷启动单次查询（进程启动 + 载入索引 + 一次查询）≤ 1.5 s；热查询按条统计 p50 ≤ 5 ms、p95 ≤ 20 ms、p99 ≤ 40 ms（空前缀同样计入）；`/api/complete` 本机回环单并发端到端 p95 ≤ 50 ms。细则见 `samples/expected-latency.txt`。

验收步骤：

1. 对同名素材执行 `build` 与 `query`，stdout 与 `samples/queries/<名>.expected.txt` 逐字节一致。
2. `manifest.json` 的 `entry_count` 等于第六节给出的条数。
3. 时间与内存按 `bench` 与外部计时复核，任一超标即不通过。
4. `samples/queries/invalid.txt` 必须 stdout 为空、退出码 1。
5. 把索引目录复制到别处后仍能查询；查询前把词表改名或移走，查询必须照常成功。

## 六、样例说明

每组素材一一对应：`samples/words/<名>.txt` 配 `samples/queries/<名>.txt` 与 `samples/queries/<名>.expected.txt`。

- `basic`（14 条）：字段与注释、缺省权重、重复词按首次出现、中文前缀；清单含 `K=0`、K 超过候选数、空前缀。
- `ranking`（13 条）：分数公式与权重影响、长度加成、三级并列打破、K 正好切在并列组内部。
- `equal-freq`（34 条）：频率全为 100，其中 30 条分数完全相同，顺序完全由码点序决定。
- `unicode`（16 条）：大小写、全半角、合成与分解形式各算不同的词，含非 BMP 字符与中文。
- `dense`（800 条）：同前缀密集，前缀 `d` 命中 800 条、`data` 命中 600 条，另有无命中前缀。
- `long`（13 条）：超长词与超长前缀，最长词与最长前缀都是 4096 码点。
- `big`（100000 条，约 1.7 MB）：十万条规模，前缀 `se` 命中 5000 条、`国` 命中 2500 条，含无命中前缀与四十码点长前缀。
- `queries/invalid.txt`：第二条查询行的 K 非法，期望 stdout 为空、退出码 1（`invalid.expected.txt` 是 0 字节文件）。

`samples/notes.md` 是现场记录，`samples/expected-latency.txt` 是延迟与内存的验收口径。

## 七、实现说明

以下为本次实现的固定口径，对应原「待补的文档」各项。

### 代码布局

- `cli.py`：命令行入口（`build` / `query` / `bench` / `serve`）。
- `engine/scoring.py`：分数公式、两位小数字符串格式化（纯整数）。
- `engine/wordsio.py`：词表与查询清单解析、非法行报错（文件:行号）。
- `engine/index.py`：索引构建、落盘、只读查询。
- `engine/server.py`：标准库 `ThreadingHTTPServer` 实现的 HTTP 接口与静态托管。
- `web/index.html`：原生 ES module 单页，无构建、无 CDN。
- `tests/test_engine.py`：`unittest` 测试，含全部样例的逐字节 CLI 回归。
- `var/`：索引与本地产物目录，已在 `.gitignore` 中。

### 索引字节布局

索引目录包含四个相对路径文件，目录可整体复制搬移，不依赖词表与绝对路径：

- `manifest.json`：UTF-8 JSON，必含 `version`（=1）、`entry_count`、
  `source_sha256`；另附 `build_ms`、`build_rss_mb`、`index_bytes`、
  `word_bytes`、`index_format`。该文件最后原子落盘，存在即代表索引可用。
- `words.bin`：8 字节魔数 `PPIXWRD1`，随后按码点序拼接的全部词 UTF-8 字节，
  不重复存储词表之外的任何字符串。
- `index.bin`：8 字节魔数 `PPIXIDX1`，随后每条词一条 24 字节定长记录
  （小端）：`S100 uint64`、`词字节偏移 uint32`、`频率 uint32`、
  `权重 uint32`、`UTF-8 字节数 uint16`、`码点数 uint16`，顺序与词码点序一致。
- `top.bin`：8 字节魔数 `PPIXTOP1`、条目数 `uint32`，随后是构建期算好的
  全局前 1000 名下标（`uint32`，按全序排名降序），供空前缀直接取用。

内存中不展开任何前缀表/trie：驻留内容只有全部词文本与每条 24 字节记录，
十万条约 3.4 MB（词文本）+ 2.4 MB（记录），远低于 512 MB 上限。

### 查询算法与复杂度

- 非空前缀：在码点序词表上用 `bisect` 做两次二分，得到候选区间
  `[lo, hi)`，定位代价 O(log n)；区间内用容量 K 的最小堆（`heapq.nlargest`
  的全序键 `(S100, 频率, -L, 词反向序)`）一次扫描取前 K，
  代价 O(m log K)，m 为命中数；任何情况下都不遍历整份词表。
- 空前缀：直接返回 `top.bin` 的前 K，O(K)，构建期预算、查询零扫描。
- 区间上界由「前缀末位码点 +1」构造；当前缀为理论最大串时上界取无穷。

### 构建原子性

目标目录不存在时：在同级临时目录写全部数据文件（每个文件先写
`*.tmp-<pid>` 再 `os.replace`），最后用 `os.replace` 整体替换目录，
再原子写入 `manifest.json`。目标目录已存在时：数据文件逐个原地原子替换，
清理由清单驱动，`manifest.json` 仍最后写入。构建非法则退出码 1，
且目标位置不会出现可用索引（无 `manifest.json`）。

### bench 计时口径

- 索引在计时前完成加载；首轮查询作为预热（每条跑一次），**不计入**统计。
- 计时只包裹 `IndexReader.complete()` 本身；同一条查询的 R 次重复各自计时，
  先在每条查询内部排序算 p50/p95/p99（线性插值），再对不同查询取最大值，
  与「每条查询分别统计后取分位，不把不同查询混在一起」的口径一致。
- `rss_mb` 取进程 `ru_maxrss`（Linux 下 KB/1024）。

### HTTP 并发、超时与错误

- `ThreadingHTTPServer`，监听 `127.0.0.1`，HTTP/1.1，keep-alive，
  socket 超时 10 秒；单并发验收口径下请求各自独立线程处理。
- `GET /api/complete?prefix=<前缀>&k=<K>`：`k` 缺省 10，仅接受
  `0..1000` 的十进制整数；`prefix` 可空、不得含空白字符、码点数 ≤ 4096。
  非法参数返回 `400 {"error": "…"}`。
- 响应固定 `application/json; charset=utf-8`，分数为与 CLI 完全一致的
  两位小数字符串；响应额外含 `elapsed_ms`（引擎查询耗时，毫秒）。
- 另提供 `GET /api/stats`：返回词条数、`source_sha256`、构建耗时
  `build_ms`、构建峰值内存 `build_rss_mb`、索引字节数 `index_bytes`
  与当前进程 RSS `query_rss_mb`，全部来自构建/运行期实测，页面不重算。

### 页面交互

- 顶部指标条渲染 `/api/stats`：词条数、构建耗时、构建峰值内存、
  当前进程内存、索引体积。
- 「单框补全」输入即查，120 ms 防抖，清空时请求空前缀（=默认推荐）。
- 「批量查询」每行 `前缀<TAB>K`，空前缀以 TAB 开头，可直接粘贴查询清单；
  每行显示返回条数与该次 `elapsed_ms`，候选按接口返回顺序展示，
  分数文本直接渲染，页面不计算任何分数。
- 无候选显示「无候选」；超长词在 chip 内 CSS 截断，悬停 title 显示全文。
- 页面无键盘导航、无高亮（README 已声明不在验收范围）。

### 版本与演进

- 当前索引 `version = 1`；`manifest.json` 版本不匹配或文件缺失时，
  载入报错并以退出码 2 退出。
- 本期不做索引格式迁移、多词表共存与在线重建；换词表请向新目录
  `build`，校验成功后再切换 `serve --index` 指向。

### 运行与测试

```
python3 cli.py build --words samples/words/big.txt --index var/index-big
python3 cli.py query --index var/index-big --queries samples/queries/big.txt
python3 cli.py bench --index var/index-big --queries samples/queries/big.txt --repeat 200
python3 cli.py serve --index var/index-big --port 8000
python3 -m unittest discover -s tests -v
```

实测（本机，十万词）：构建约 0.1 s（含落盘），索引约 3.57 MB（词表 1.74 MB，
小于 3 倍上限），查询进程 RSS 约 31 MB；热查询 p95 ≈ 2.4 ms，
回环接口 p95 ≈ 3.2 ms，冷启动整进程约 0.05 s，全部留有充分余量。
