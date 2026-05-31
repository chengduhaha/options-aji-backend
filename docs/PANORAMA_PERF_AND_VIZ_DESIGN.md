# 产业星图 — 性能优化方案 & 前端展示设计

> 配套文档：`docs/PRODUCT_SUPPLY_CHAIN_GRAPH_PRD.md`（产品 & 技术设计）
> 本文聚焦两件事：**① 加载慢的根因与优化**、**② 最佳前端图谱展示设计**。
> 状态：设计稿 v1（基于线上 /panorama 实测链路分析，未改代码）。

---

## Part 1 — 加载慢的根因与优化

### 1.1 实测链路

线上每次加载产业星图的请求链路：

```
浏览器 ──fetch(cache:"no-store")──> Vercel Serverless 函数(/api/graph 代理, runtime=nodejs)
        ──跨区域网络 + TLS──> 云服务器后端(FastAPI) ──> PostgreSQL(递归 CTE)
```

且 `PanoramaClient` 挂载时**并行发起 3 个**这样的全链路请求（`graph` + `views` + `timeline`），
全部 `cache:"no-store"`，每次改查询参数还会重新全量拉取。

### 1.2 耗时归因

| 耗时源 | 说明 | 量级 |
|---|---|---|
| ① Vercel 函数冷启动 | nodejs 运行时代理函数空闲后冷启 | 数百 ms ~ 1s+ |
| ② Vercel→后端跨区域 RTT | Vercel 函数默认跑美国/全球边缘，后端单机在国内 → 每次往返 200~400ms+ 加 TLS 握手 | **主要瓶颈** |
| ③ 挂载并行 3 请求 + no-store | 各走一遍全链路，禁用一切缓存；改参数重新全量拉 | ×3、×每次交互 |
| ④ 后端单体冷启 | 进程 import futu/yfinance/langchain/discord/stripe/apscheduler 等重依赖 | 不定 |
| ⑤ PostgreSQL 查询 | 递归 CTE，26 节点 | **<5ms，可忽略** |

> **结论：瓶颈在网络层（①②③④），不是存储（⑤）。**

### 1.3 关于"换内嵌图数据库"的评估

换存储能解决的是「图原生查询体验 / 本地零运维」，**解决不了当前的慢**（慢在网络往返与冷启动）。
把 PostgreSQL 换成内嵌图库，只会优化已是微秒级的⑤，对①②③④无帮助，且增加复杂度。

**结论：存储保持 PostgreSQL 不动**（可靠事实来源 + 已有 Alembic 迁移），优化重心放在消灭网络往返。

若未来确需图原生开发体验，内嵌方案选型梯队：

| 方案 | 定位 | 评价 |
|---|---|---|
| **Kùzu** (`pip install kuzu`) | "图界的 SQLite"，嵌入式、持久化、支持 Cypher、列存极快 | ⭐ 想要图原生+零运维的最佳选择 |
| NetworkX | 纯 Python 内存图库，算法丰富，非持久化 | 适合图算法/分析，不当主存储 |
| RustworkX | Rust 内核内存图，算法快 | 大图算法场景 |
| DuckDB | 嵌入式分析库，可做递归/属性图 | 偏分析统计 |
| Neo4j / Memgraph | 服务进程，非嵌入 | 现阶段过重，不推荐 |

### 1.4 优化方案（按性价比排序）

1. **🥇 策展图静态化（最猛）**
   数据为人工 ingest、几乎不变。在 ingest 时**预生成每张策展图的静态 JSON**，
   前端直接从 Vercel CDN 拉取 → 加载从秒级变毫秒级、零后端往返、零冷启动。
   对"读多写少的策展图"是降维打击。

2. **🥈 合并请求 + 开启缓存**
   将 `graph` + `views` + `timeline` 合并为 1 个端点；去掉 `cache:"no-store"`，
   改 `Cache-Control: s-maxage=300, stale-while-revalidate`，利用 Vercel 边缘缓存。
   命中缓存后 0 次回源。

3. **🥉 全图入内存 / Redis**
   后端启动时把整张图（几百节点）load 进内存 dict 或 NetworkX，子图遍历在内存完成；
   Redis 存整图 blob。DB 彻底移出热路径。

4. **懒加载展开**
   首屏只渲染 focus + 一跳，点击节点再 `expand` 拉取邻居（Neo4j Bloom 模式），首屏瞬开。

5. **后端就近 / 边缘缓存**
   若用户主要在国内，将 Vercel 函数 region 设到离后端近处，或给后端套 CDN/边缘缓存。

> 落地优先级：先做 **静态化 + 缓存 + 合并请求**，预计当场消除 ~90% 的加载延迟。

---

## Part 2 — 最佳前端图谱展示设计

> 抛开现有页面风格约束，纯粹追求最佳图谱展示。
> 核心矛盾：**富节点卡片（logo/股价/护城河）** vs **规模与丝滑度**。

### 2.1 渲染层选型

| 渲染层 | 代表库 | 节点上限 | 富卡片 | 星空炫感 |
|---|---|---|---|---|
| SVG/HTML | **React Flow**（当前方案） | ~200 | ⭐⭐⭐ 任意 HTML 卡片 | ⭐ |
| Canvas 2D | Cytoscape.js / react-force-graph 2D | 数千 | ⭐⭐ 图形+图片+标签 | ⭐⭐ |
| **WebGL/GPU** | react-force-graph(WebGL) / Sigma.js / Cosmograph / G6 v5 | 数万+ | ⭐ canvas 手绘 | ⭐⭐⭐ |

### 2.2 业界标杆参考

- **Neo4j Bloom** — 图数据库探索黄金标准：focus + 展开、邻居高亮、气泡卡片
- **Cosmograph** — GPU 力导向"星系"美学，粒子流光，海量点丝滑
- **Obsidian 关系图** — 极简、呼吸感的力导向美学
- **Kumu / GraphXR / Linkurious** — 专业知识图谱探索器

### 2.3 推荐方案：双引擎按场景分工

**A. 公司 ego 视角（主打，富卡片）→ 继续 React Flow，升级为"探索器"而非全量树**

关键升级（把"能看"变"惊艳"的点）：
1. **语义缩放 LOD**：放大显示完整卡片（logo+sparkline+护城河），缩小自动退化为发光圆点+ticker，
   兼顾清爽、纵览与性能。
2. **懒展开探索**：进入只显示 focus + 分部，点击节点像涟漪般展开下一跳（配展开动画），
   首屏瞬开 + 探索叙事感。
3. **邻居聚光**：hover 节点高亮其边与邻居，其余变暗（focus + context）。
4. **有向流光粒子**：边上跑光点表示供应流向（react-force-graph 招牌效果），独家关系金色粒子。
5. **分部星座**：每个业务分部用凸包/光晕色块圈起（constellation hull），
   AI=紫 / Connectivity=青 / Space=蓝，"分散有序"一眼可读。
6. **聚光灯 + 辉光**：focus 节点中心打光；护城河 exclusive 节点 bloom 发光。
7. 深空背景 + 视差星点 + 玻璃拟态卡片。

**B. 行业大图 / 全景星空视角 → react-force-graph(WebGL) 或 Cosmograph / G6 v5(WebGL)**

节点上百上千时用 GPU 力导向呈现"星系"效果：粒子流光、景深、丝滑缩放、节点 logo 贴图。
注：项目已依赖 **G6 v5**（含 WebGL + 力导向 + 鱼眼 + 边绑定），行业视角可直接用它。

### 2.4 取舍建议

- **节点长期 < 200**：留在 React Flow，做满 2.3-A 的 1~7 项即可"富卡片 + 丝滑炫酷"，改动最小、收益最大。
- **要上百上千 / 追求星系级视觉**：公司视角 React Flow，全景/行业视角换 WebGL（react-force-graph 或 G6 v5 WebGL）双引擎。

### 2.5 性能必做项

1. 用**轻量内联 SVG sparkline** 替换每个节点里的 recharts `ResponsiveContainer`
   （26 个 ResponsiveContainer 是渲染卡顿元凶之一）。
2. 节点组件 **memo 化**，避免力导向 tick 时全量重渲染。
3. **LOD 降级**：缩小到阈值以下时只画圆点。

---

## 总结

- **存储**：不换。PostgreSQL 没问题；慢在网络层，做"静态化 + 缓存 + 懒加载"。
- **前端**：< 200 点把 React Flow 升级为"探索器"（LOD + 懒展开 + 聚光 + 流光粒子 + 分部星座）；
  要星系级规模再上 WebGL 双引擎。
