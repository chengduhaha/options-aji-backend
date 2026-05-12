# Massive + FMP 全量 API 端点参考

> 只列你已订阅层级可调用的所有端点。不做产品映射，纯数据源参考。

---

# Part 1: Massive — Options Starter ($29/月)

```
Base URL: https://api.massive.com
认证: Header — Authorization: Bearer {API_KEY}
限制: Unlimited API Calls
延迟: 15 分钟延迟（Snapshot/Aggregates）
历史: 2 年（合约参考数据可追溯到 2014 年）
```

---

## REST API — Options Contracts (参考数据)

### 1. All Contracts — 所有期权合约索引
```
GET /v3/reference/options/contracts

查询参数:
  underlying_ticker   string    按标的筛选（如 SPY）
  contract_type       enum      call / put
  expiration_date     string    YYYY-MM-DD，支持 .gt .gte .lt .lte 修饰符
  strike_price        number    支持 .gt .gte .lt .lte 修饰符
  expired             boolean   是否包含已到期合约，默认 false
  as_of               string    YYYY-MM-DD，查看某日时点的合约
  order               enum      asc / desc
  sort                enum      排序字段
  limit               integer   最大 1000

返回字段:
  ticker              期权合约代码（如 O:SPY260516C00550000）
  underlying_ticker   标的代码
  contract_type       call / put
  exercise_style      american / european / bermudan
  expiration_date     到期日
  strike_price        行权价
  shares_per_contract 每张合约股数（通常 100）
  primary_exchange    上市交易所 MIC 码
  cfi                 ISO 10962 CFI 码
  additional_underlyings  额外交割物（拆股/并购等特殊情况）

数据时效: 每日更新
历史深度: 全部历史（追溯到 2014-06-02）
```

### 2. Contract Overview — 单个合约详情
```
GET /v3/reference/options/contracts/{optionsTicker}

路径参数:
  optionsTicker       期权合约代码

返回字段: 同上（单个合约的完整参考信息）

历史深度: 全部历史
```

---

## REST API — Options Aggregates (K 线聚合)

### 3. Custom Bars — 自定义时间粒度 K 线
```
GET /v2/aggs/ticker/{optionsTicker}/range/{multiplier}/{timespan}/{from}/{to}

路径参数:
  optionsTicker       期权合约代码
  multiplier          时间倍数（如 1, 5, 15）
  timespan            minute / hour / day / week / month / quarter / year
  from                起始日期 YYYY-MM-DD 或时间戳
  to                  结束日期 YYYY-MM-DD 或时间戳

查询参数:
  adjusted            boolean   是否调整拆股等
  sort                asc / desc
  limit               integer   最大 50000

返回字段:
  o                   开盘价
  h                   最高价
  l                   最低价
  c                   收盘价
  v                   成交量（合约数）
  vw                  成交量加权平均价
  t                   时间戳（Unix ms）
  n                   成交笔数

数据时效: 15 分钟延迟
```

### 4. Daily Ticker Summary — 单日汇总
```
GET /v2/aggs/ticker/{optionsTicker}/range/1/day/{date}/{date}

返回: 该合约当日的 OHLCV + VWAP

数据时效: 15 分钟延迟
```

### 5. Previous Day Bar — 前一交易日汇总
```
GET /v2/aggs/ticker/{optionsTicker}/prev

查询参数:
  adjusted            boolean

返回字段: o, h, l, c, v, vw, t, n

数据时效: 15 分钟延迟
```

---

## REST API — Options Snapshots (快照)

### 6. Option Contract Snapshot — 单个合约快照 ⭐
```
GET /v3/snapshot/options/{underlyingAsset}

路径参数:
  underlyingAsset     标的代码（如 SPY）

查询参数:
  strike_price        number    按行权价筛选，支持修饰符
  expiration_date     string    YYYY-MM-DD，按到期日筛选，支持修饰符
  contract_type       enum      call / put
  order               enum      asc / desc
  sort                enum      排序字段
  limit               integer   最大 250

返回字段:
  break_even_price    盈亏平衡价
  day                 当日 K 线
    .open / .high / .low / .close / .volume / .vwap
    .change / .change_percent    当日涨跌
    .previous_close              前收
  details             合约详情
    .contract_type / .exercise_style / .expiration_date
    .shares_per_contract / .strike_price / .ticker
  greeks              希腊字母 ⭐
    .delta / .gamma / .theta / .vega
  implied_volatility  隐含波动率 ⭐
  open_interest       持仓量 ⭐
  last_quote          最新报价
    .bid / .ask / .bid_size / .ask_size
    .midpoint / .timeframe / .last_updated
  last_trade          最新成交（Starter 层可能不含）
    .price / .size / .exchange / .conditions / .timeframe
  underlying_asset    标的资产信息
    .price / .change_to_break_even / .ticker / .timeframe

分页: 超过 limit 时返回 next_url，需持续请求
数据时效: 15 分钟延迟
```

### 7. Option Chain Snapshot — 完整期权链快照 ⭐⭐⭐ 最核心
```
与上面端点相同:
GET /v3/snapshot/options/{underlyingAsset}

不带 strike_price/contract_type 筛选 = 返回该标的的完整期权链
带 expiration_date = 返回指定到期日的完整链

返回字段: 同上（数组，每个元素是一个合约的完整快照）
```

### 8. Unified Snapshot — 统一快照（跨资产类型）
```
GET /v3/snapshot

查询参数:
  ticker              string    ticker 筛选，支持 .any_of 修饰符
  type                enum      stocks / options / forex / crypto / indices
  order               enum      asc / desc
  sort                enum      排序字段
  limit               integer   最大 250

返回字段:
  ticker / type / name / market_status
  session             交易时段数据
    .change / .change_percent / .close / .high / .low
    .open / .previous_close / .volume
    .early_trading_change / .late_trading_change
  greeks / implied_volatility / open_interest（期权类型时）
  break_even_price / details / underlying_asset（期权类型时）

数据时效: 15 分钟延迟
```

---

## WebSocket API — Options

### 9. Aggregates Per Minute — 分钟级推送
```
连接: wss://delayed.massive.com/options

认证:
  {"action": "auth", "params": "{API_KEY}"}

订阅:
  {"action": "subscribe", "params": "AM.O:SPY*"}   # SPY 所有期权合约
  {"action": "subscribe", "params": "AM.O:SPY260516C00550000"}  # 单个合约

消息格式:
  ev    事件类型 "AM"
  sym   合约代码
  o / h / l / c / v   OHLCV
  vw    VWAP
  s     窗口开始时间戳
  e     窗口结束时间戳
  z     成交量（合约数）

数据时效: 15 分钟延迟
```

### 10. Aggregates Per Second — 秒级推送
```
连接: wss://delayed.massive.com/options

订阅:
  {"action": "subscribe", "params": "A.O:SPY260516C00550000"}  # 注意是 "A" 不是 "AM"

消息格式: 同分钟级

数据时效: 15 分钟延迟
```

---

## Flat Files — Options 批量文件下载

### 11. Day Aggregates — 日线聚合文件
```
格式: CSV / Parquet
内容: 所有期权合约的当日 OHLCV
更新: 每个交易日 11:00 AM ET（包含前一天数据）
用途: 构建本地历史数据库、批量回测
```

### 12. Minute Aggregates — 分钟线聚合文件
```
格式: CSV / Parquet
内容: 所有期权合约的分钟级 OHLCV
更新: 每个交易日 11:00 AM ET（包含前一天数据）
用途: 精细回测、盘中分析
```

---

# Part 2: FMP — Starter ($22/月)

```
Base URL: https://financialmodelingprep.com/stable
认证: Query — ?apikey={API_KEY}  或  Header — apikey: {API_KEY}
限制: 300 calls/分钟
覆盖: US 市场
历史: 最多 5 年
```

---

## 1. Company Search 公司搜索

```
GET /stable/search-symbol?query=AAPL             按代码搜索
GET /stable/search-name?query=Apple               按公司名搜索
GET /stable/search-cik?cik=320193                 按 CIK 搜索
GET /stable/search-cusip?cusip=037833100          按 CUSIP 搜索
GET /stable/search-isin?isin=US0378331005         按 ISIN 搜索
GET /stable/company-screener                      股票筛选器
    ?marketCapMoreThan=&sector=&industry=&exchange=&country=&volume=&beta=&price=
GET /stable/search-exchange-variants?symbol=AAPL  跨交易所查找
```

## 2. Stock Directory 股票目录

```
GET /stable/stock-list                            所有股票代码列表
GET /stable/financial-statement-symbol-list        有财报的公司列表
GET /stable/cik-list?page=0&limit=1000            CIK 列表
GET /stable/symbol-change                         代码变更记录
GET /stable/etf-list                              ETF 列表
GET /stable/actively-trading-list                 活跃交易列表
GET /stable/earnings-transcript-list              有财报转录的公司列表
GET /stable/available-exchanges                   可用交易所
GET /stable/available-sectors                     可用板块
GET /stable/available-industries                  可用行业
GET /stable/available-countries                   可用国家
```

## 3. Company Information 公司信息

```
GET /stable/profile?symbol=AAPL                   公司详细资料
    返回: companyName, price, marketCap, industry, sector, description,
          ceo, fullTimeEmployees, website, image, ipoDate, ...
GET /stable/profile-cik?cik=320193                按 CIK 查资料
GET /stable/company-notes?symbol=AAPL             公司票据信息
GET /stable/stock-peers?symbol=AAPL               同行公司对比
GET /stable/delisted-companies?page=0             退市公司列表
GET /stable/employee-count?symbol=AAPL            员工数量
GET /stable/historical-employee-count?symbol=AAPL 历史员工数量
GET /stable/market-capitalization?symbol=AAPL     市值
GET /stable/market-capitalization-batch?symbols=AAPL,MSFT  批量市值
GET /stable/historical-market-capitalization?symbol=AAPL   历史市值
GET /stable/shares-float?symbol=AAPL              流通股
GET /stable/shares-float-all?page=0               全部流通股数据
GET /stable/mergers-acquisitions-latest?page=0    最新并购
GET /stable/mergers-acquisitions-search?name=Apple 搜索并购
GET /stable/key-executives?symbol=AAPL            高管信息
GET /stable/governance-executive-compensation?symbol=AAPL  高管薪酬
GET /stable/executive-compensation-benchmark       高管薪酬基准
```

## 4. Quote 行情报价

```
GET /stable/quote?symbol=AAPL                     完整行情
    返回: price, change, changesPercentage, dayHigh, dayLow,
          yearHigh, yearLow, volume, avgVolume, marketCap, pe, eps,
          earningsAnnouncement, sharesOutstanding, ...
GET /stable/quote-short?symbol=AAPL               简短行情（price, volume, change%）
GET /stable/batch-quote-short?symbols=AAPL,MSFT,SPY  批量简短行情
GET /stable/aftermarket-trade?symbol=AAPL         盘后交易
GET /stable/aftermarket-quote?symbol=AAPL         盘后报价
GET /stable/quote-change?symbol=AAPL              价格变化（1D/5D/1M/3M/6M/YTD/1Y/3Y/5Y/10Y/MAX）
GET /stable/stock-batch-quote?symbols=AAPL,MSFT   批量完整行情
GET /stable/batch-aftermarket-trade?symbols=AAPL,MSFT  批量盘后交易
GET /stable/batch-aftermarket-quote?symbols=AAPL,MSFT  批量盘后报价
GET /stable/exchange-stock-quotes?exchange=NASDAQ  按交易所批量报价
GET /stable/mutual-fund-price-quotes              共同基金报价
GET /stable/full-commodity-quotes                 大宗商品报价
GET /stable/full-cryptocurrency-quotes            加密货币报价
GET /stable/full-forex-quote                      外汇报价
GET /stable/full-index-quotes                     指数报价
GET /stable/etf-price-quotes                      ETF 报价
```

## 5. Financial Statements 财务报表

```
GET /stable/income-statement?symbol=AAPL&period=quarter      利润表
GET /stable/income-statement-ttm?symbol=AAPL                 TTM 利润表
GET /stable/balance-sheet-statement?symbol=AAPL&period=quarter  资产负债表
GET /stable/balance-sheet-statement-ttm?symbol=AAPL           TTM 资产负债表
GET /stable/cash-flow-statement?symbol=AAPL&period=quarter    现金流量表
GET /stable/cash-flow-statement-ttm?symbol=AAPL               TTM 现金流量表
GET /stable/key-metrics?symbol=AAPL&period=quarter            关键指标
GET /stable/key-metrics-ttm?symbol=AAPL                       TTM 关键指标
GET /stable/financial-ratios?symbol=AAPL&period=quarter       财务比率
GET /stable/financial-ratios-ttm?symbol=AAPL                   TTM 财务比率
GET /stable/financial-scores?symbol=AAPL                      财务评分（Altman/Piotroski）
GET /stable/enterprise-values?symbol=AAPL&period=quarter      企业价值
GET /stable/owner-earnings?symbol=AAPL                        所有者收益
GET /stable/income-statement-growth?symbol=AAPL&period=quarter  利润增长
GET /stable/balance-sheet-statement-growth?symbol=AAPL        资产负债表增长
GET /stable/cash-flow-statement-growth?symbol=AAPL            现金流增长
GET /stable/financial-statement-growth?symbol=AAPL            综合增长
GET /stable/financial-reports-dates?symbol=AAPL               财报日期列表
GET /stable/financial-reports-json?symbol=AAPL&year=2024&period=Q3   10-K JSON
GET /stable/financial-reports-xlsx?symbol=AAPL&year=2024&period=Q3   10-K Excel
GET /stable/revenue-product-segmentation?symbol=AAPL          收入产品分类
GET /stable/revenue-geographic-segmentation?symbol=AAPL       收入地区分类
GET /stable/as-reported-income-statements?symbol=AAPL         原始利润表
GET /stable/as-reported-balance-sheet-statements?symbol=AAPL  原始资产负债表
GET /stable/as-reported-cash-flow-statements?symbol=AAPL      原始现金流量表
GET /stable/as-reported-financial-statements?symbol=AAPL      原始综合财报
```

## 6. Discounted Cash Flow 现金流折现

```
GET /stable/discounted-cash-flow?symbol=AAPL               DCF 估值
GET /stable/levered-discounted-cash-flow?symbol=AAPL       杠杆 DCF
GET /stable/advanced-discounted-cash-flow?symbol=AAPL       高级 DCF
GET /stable/advanced-levered-discounted-cash-flow?symbol=AAPL  高级杠杆 DCF
```

## 7. Charts 图表/历史价格

```
GET /stable/historical-price-eod/full/{symbol}?from=&to=    日线历史
GET /stable/historical-price-eod/light/{symbol}?from=&to=   轻量日线
GET /stable/stock-price-undajusted/{symbol}                 未调整价格
GET /stable/stock-price-and-volume/{symbol}                 价格+成交量
GET /stable/dividend-adjusted-price/{symbol}                分红调整价格
GET /stable/historical-chart/1min/{symbol}?from=&to=        1 分钟 K 线
GET /stable/historical-chart/5min/{symbol}?from=&to=        5 分钟 K 线
GET /stable/historical-chart/15min/{symbol}?from=&to=       15 分钟 K 线
GET /stable/historical-chart/30min/{symbol}?from=&to=       30 分钟 K 线
GET /stable/historical-chart/1hour/{symbol}?from=&to=       1 小时 K 线
GET /stable/historical-chart/4hour/{symbol}?from=&to=       4 小时 K 线
```

## 8. Economics 宏观经济

```
GET /stable/treasury-rates?from=&to=              国债利率
GET /stable/economics-indicators?name=GDP          经济指标（GDP/CPI/unemployment 等）
GET /stable/economic-calendar?from=&to=            经济日历（FOMC/CPI/非农等）
GET /stable/market-risk-premium                    市场风险溢价
```

## 9. Earnings, Dividends, Splits 财报/分红/拆股

```
GET /stable/dividends?symbol=AAPL                 分红记录
GET /stable/dividends-calendar?from=&to=           分红日历
GET /stable/earnings-report?symbol=AAPL            财报详情
GET /stable/earnings-calendar?from=&to=            财报日历 ⭐
GET /stable/earnings-surprises/{symbol}            EPS surprise 历史
GET /stable/ipo-calendar?from=&to=                 IPO 日历
GET /stable/ipo-disclosure?symbol=                 IPO 披露
GET /stable/ipo-prospectus?symbol=                 IPO 招股书
GET /stable/stock-split-details?symbol=AAPL        拆股详情
GET /stable/stock-splits-calendar?from=&to=        拆股日历
```

## 10. Earnings Transcript 财报电话会议

```
GET /stable/earnings-transcript-latest?page=0      最新转录
GET /stable/earnings-transcript?symbol=AAPL&year=2024&quarter=4  指定季度转录
GET /stable/earnings-transcript-dates?symbol=AAPL  可用转录日期
GET /stable/earnings-transcript-list                可用转录公司列表

注意: Starter 层可能是 Limited Access
```

## 11. News 新闻

```
GET /stable/fmp-articles?page=0                   FMP 自有文章
GET /stable/news/general?page=0                   综合新闻
GET /stable/news/stock?tickers=AAPL,TSLA&page=0   个股新闻 ⭐
GET /stable/news/press-releases?symbol=AAPL        新闻稿
GET /stable/news/crypto?page=0                     加密新闻
GET /stable/news/forex?page=0                      外汇新闻
GET /stable/search-news/stock?query=tariff          搜索股票新闻
GET /stable/search-news/crypto?query=bitcoin        搜索加密新闻
GET /stable/search-news/forex?query=dollar          搜索外汇新闻
GET /stable/search-news/press-releases?query=Apple  搜索新闻稿
```

## 12. Analyst 分析师

```
GET /stable/financial-estimates?symbol=AAPL        财务预估（EPS/Revenue）
GET /stable/ratings-snapshot?symbol=AAPL           评级快照
GET /stable/historical-ratings?symbol=AAPL         历史评级
GET /stable/price-target-summary?symbol=AAPL       目标价汇总
GET /stable/price-target-consensus?symbol=AAPL     目标价共识
GET /stable/stock-grades?symbol=AAPL               分析师评级变更
GET /stable/historical-stock-grades?symbol=AAPL    历史评级变更
GET /stable/stock-grades-summary?symbol=AAPL       评级汇总
```

## 13. Market Performance 市场表现

```
GET /stable/sector-performance                     板块表现 ⭐
GET /stable/industry-performance                   行业表现
GET /stable/historical-sector-performance           历史板块表现
GET /stable/historical-industry-performance         历史行业表现
GET /stable/sector-pe-snapshot                     板块 PE
GET /stable/industry-pe-snapshot                   行业 PE
GET /stable/historical-sector-pe                   历史板块 PE
GET /stable/historical-industry-pe                 历史行业 PE
GET /stable/stock-market-gainers                   涨幅榜 ⭐
GET /stable/stock-market-losers                    跌幅榜 ⭐
GET /stable/stock-market-most-actives              最活跃 ⭐
```

## 14. Technical Indicators 技术指标

```
GET /stable/technical-indicator/daily/{symbol}?type={type}&period={n}

type 可选值:
  sma     简单移动平均线
  ema     指数移动平均线
  dema    双指数移动平均线
  tema    三指数移动平均线
  wma     加权移动平均线
  rsi     相对强弱指标
  williams Williams %R
  adx     平均方向指标
  stdev   标准差

注意: Starter 层可能是 Limited Access
```

## 15. ETF & Mutual Funds

```
GET /stable/etf-holdings?symbol=SPY               ETF 持仓 ⭐
GET /stable/etf-mutual-fund-info?symbol=SPY        ETF/基金信息
GET /stable/etf-country-allocation?symbol=SPY      国家配置
GET /stable/etf-asset-exposure?symbol=SPY          资产敞口
GET /stable/etf-sector-weighting?symbol=SPY        板块权重
GET /stable/mutual-fund-etf-disclosure?symbol=     基金披露
GET /stable/mutual-fund-disclosure-name-search?name= 基金名称搜索
GET /stable/fund-etf-disclosures-by-date?date=     按日期查披露
```

## 16. SEC Filings SEC 文件

```
GET /stable/sec-filings-latest-8k?page=0           最新 8-K 文件
GET /stable/sec-filings-latest?page=0               最新 SEC 文件
GET /stable/sec-filings-by-type?type=10-K           按类型查
GET /stable/sec-filings-by-symbol?symbol=AAPL       按公司查
GET /stable/sec-filings-by-cik?cik=320193           按 CIK 查
GET /stable/sec-filings-by-name?name=Apple          按名称查
GET /stable/sec-filings-search-by-symbol?symbol=AAPL&query=  搜索
GET /stable/sec-filings-search-by-cik?cik=&query=   按 CIK 搜索
GET /stable/sec-company-full-profile?symbol=AAPL    SEC 完整资料
GET /stable/industry-classification-list             行业分类列表
GET /stable/industry-classification-search?query=    搜索行业分类
GET /stable/all-industry-classification              全部行业分类
```

## 17. Insider Trades 内部人交易

```
GET /stable/insider-trading?symbol=AAPL&page=0      内部人交易记录 ⭐
GET /stable/search-insider-trading?name=Tim+Cook     搜索内部人
GET /stable/search-insider-trading-by-reporting-name?name=  按报告人搜索
GET /stable/all-insider-transaction-types             交易类型列表
GET /stable/insider-trade-statistics?symbol=AAPL     交易统计
GET /stable/acquisition-ownership?symbol=AAPL        收购持股
```

## 18. Form 13F 机构持仓

```
GET /stable/form-thirteenf?symbol=AAPL              按公司查 13F ⭐
GET /stable/form-thirteenf?cik=0001067983            按 CIK 查（如 Berkshire）
GET /stable/form-thirteenf-extract?cik=              持仓提取
GET /stable/form-thirteenf-dates?cik=                13F 日期列表
GET /stable/form-thirteenf-extract-with-analytics?cik=  含分析的提取
GET /stable/form-thirteenf-holder-performance?cik=   持有人表现
GET /stable/form-thirteenf-holders-industry-breakdown?cik=  行业分布
GET /stable/form-thirteenf-positions-summary?cik=    持仓汇总
GET /stable/form-thirteenf-industry-performance?cik= 行业表现
```

## 19. Senate & House 国会议员交易

```
GET /stable/senate-latest-trading?page=0             参议员最新交易 ⭐
GET /stable/house-latest-trading?page=0              众议员最新交易
GET /stable/senate-trading-activity?symbol=AAPL      参议员交易活动
GET /stable/senate-financial-disclosures-latest?page=0  参议员财务披露
GET /stable/house-financial-disclosures-latest?page=0   众议员财务披露
GET /stable/senate-trades-by-name?name=              按参议员查
GET /stable/house-trades-by-name?name=               按众议员查
```

## 20. Indexes 指数

```
GET /stable/index-list                             指数列表
GET /stable/index-quote?symbol=%5EGSPC             指数报价（^GSPC = S&P 500）
GET /stable/index-quote-short?symbol=%5EGSPC       简短指数报价
GET /stable/all-index-quotes                       全部指数报价
GET /stable/historical-index-chart-light/{symbol}   历史指数（轻量）
GET /stable/historical-index-chart-full/{symbol}    历史指数（完整）
GET /stable/1-minute-index-price/{symbol}          1 分钟指数价格
GET /stable/5-minute-index-price/{symbol}          5 分钟指数价格
GET /stable/1-hour-index-price/{symbol}            1 小时指数价格
GET /stable/sp500-index                            S&P 500 成分股
GET /stable/nasdaq-index                           Nasdaq 成分股
GET /stable/dow-jones-index                        道琼斯成分股
GET /stable/historical-sp500?from=&to=             历史 S&P 500
GET /stable/historical-nasdaq?from=&to=            历史 Nasdaq
GET /stable/historical-dow-jones?from=&to=         历史道琼斯
```

## 21. Market Hours 市场时间

```
GET /stable/global-exchange-market-hours            全球交易所时间
GET /stable/exchange-holidays?exchange=NYSE          交易所假日
GET /stable/all-exchange-market-hours               所有交易所时间
GET /stable/is-the-market-open                      市场是否开盘 ⭐
```

## 22. Commodity 大宗商品

```
GET /stable/commodities-list                       商品列表
GET /stable/commodity-quote?symbol=GCUSD            商品报价（黄金等）
GET /stable/commodity-quote-short?symbol=GCUSD      简短报价
GET /stable/all-commodity-quotes                    全部商品报价
GET /stable/commodity-chart-light/{symbol}          历史（轻量）
GET /stable/commodity-chart-full/{symbol}           历史（完整）
GET /stable/1-minute-commodity-chart/{symbol}       1 分钟
GET /stable/5-minute-commodity-chart/{symbol}       5 分钟
GET /stable/1-hour-commodity-chart/{symbol}         1 小时
```

## 23. Forex 外汇

```
GET /stable/forex-currency-pairs                   货币对列表
GET /stable/forex-quote?symbol=EURUSD              外汇报价
GET /stable/forex-quote-short?symbol=EURUSD        简短报价
GET /stable/batch-forex-quotes                     批量报价
GET /stable/historical-forex-chart-light/{symbol}  历史（轻量）
GET /stable/historical-forex-chart-full/{symbol}   历史（完整）
GET /stable/1-minute-forex-chart/{symbol}          1 分钟
GET /stable/5-minute-forex-chart/{symbol}          5 分钟
GET /stable/1-hour-forex-chart/{symbol}            1 小时
```

## 24. Crypto 加密货币

```
GET /stable/cryptocurrency-list                    加密货币列表
GET /stable/full-cryptocurrency-quote?symbol=BTCUSD 完整报价
GET /stable/cryptocurrency-quote-short?symbol=BTCUSD 简短报价
GET /stable/all-cryptocurrency-quotes              全部报价
GET /stable/historical-cryptocurrency-chart-light/{symbol}  历史
GET /stable/historical-cryptocurrency-chart-full/{symbol}   历史（完整）
GET /stable/1-minute-cryptocurrency-data/{symbol}  1 分钟
GET /stable/5-minute-cryptocurrency-data/{symbol}  5 分钟
GET /stable/1-hour-cryptocurrency-data/{symbol}    1 小时
```

## 25. Commitment of Traders (COT) 交易商持仓报告

```
GET /stable/cot-report?from=&to=                   COT 报告
GET /stable/cot-analysis?from=&to=                  COT 分析
GET /stable/cot-report-list                         COT 报告列表
```

## 26. Fundraisers 众筹/股权融资

```
GET /stable/crowdfunding-latest?page=0              最新众筹
GET /stable/crowdfunding-search?query=              搜索众筹
GET /stable/crowdfunding-by-cik?cik=                按 CIK 查
GET /stable/equity-offering-updates?page=0          股权发行更新
GET /stable/equity-offering-search?query=           搜索股权发行
GET /stable/equity-offering-by-cik?cik=             按 CIK 查股权发行
```

## 27. Bulk 批量数据

```
GET /stable/company-profile-bulk                   批量公司资料
GET /stable/stock-rating-bulk                      批量评级
GET /stable/dcf-valuations-bulk                    批量 DCF
GET /stable/financial-scores-bulk                  批量财务评分
GET /stable/price-target-summary-bulk              批量目标价
GET /stable/etf-holder-bulk                        批量 ETF 持仓
GET /stable/key-metrics-ttm-bulk                   批量 TTM 指标
GET /stable/ratios-ttm-bulk                        批量 TTM 比率
GET /stable/upgrades-downgrades-consensus-bulk     批量升降级
GET /stable/stock-peers-bulk                       批量同行
GET /stable/earnings-surprises-bulk                批量 EPS surprise
GET /stable/income-statement-bulk?period=quarter   批量利润表
GET /stable/balance-sheet-statement-growth-bulk    批量资产负债表增长
GET /stable/income-statement-growth-bulk           批量利润增长
GET /stable/cash-flow-statement-growth-bulk        批量现金流增长
GET /stable/eod-bulk                               批量 EOD 价格
```