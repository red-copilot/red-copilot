# discovery.vuln-scan

domain: web  
tags: discovery, vuln, sqli, xss, ssrf, injection

Automated vulnerability scanning — injection, SSRF, auth bypass, and deserialization.

## 何时使用

- `observation:web.endpoint`（信任级别 ≥ claim）

## 预期结论

- `hypothesis:web.vulnerability`
- `observation:web.finding`

## 侦察与判定信号

- 输入点优先于工具输出：拿到接口与参数清单后，按"输入 → 处理 → 输出"的路径选检查项，
  而不是把扫描器默认模板全量跑一遍。
- 组件与版本：已知组件的公开漏洞只有能确认版本且能触达对应功能时才值得跟进。
  只报版本号不做验证的结论会被平台判为无效。
- 配置面：目录列举、备份文件、调试端点、默认凭据、错误回显。
- 判定信号：一次扫描产生的是"候选清单"（要验证什么），不是漏洞结论。

## payload 家族与变体

- 注入族：SQL 注入（联合/布尔/时间盲注）、命令注入（分隔符与编码变体）、模板注入、LDAP/XPATH。
- 文件族：路径穿越、任意文件读取、文件上传（扩展名/内容类型/解析链）。
- 服务端请求族：SSRF（协议、内网地址、云元数据、DNS 重绑定）。
- 客户端族：反射与存储型 XSS（上下文：HTML/属性/JS/URL）、CSRF、开放重定向。
- 组件族：已知 CVE 的公开 PoC，优先使用最小化的探测变体，不用破坏性利用链。
- 变体原则：每个族先试最轻量的探测载荷，确认信号后再加深；编码、大小写、注释符、参数位置
  （query/body/header/cookie/path）都是变体维度。

## 预期证据形态

- `hypothesis:vuln.suspected`：把"哪个输入、什么处理、预期什么信号"写清楚。
- `observation:web.finding`：包含可复现的请求、关键响应片段与判定依据。
- 每个候选都要带"可证伪条件"：出现什么现象就说明这条假设不成立。

## 操作边界

- 只在授权目标与授权接口上测试；不对第三方组件后端、外部回调地址发恶意载荷。
- 禁止破坏性载荷（`DROP`、删文件、写 webshell 之外的持久化）、禁止高并发盲注与 DoS。
- 时间型盲注要限制延迟与次数，避免把目标拖死。
- 数据泄露类测试最多验证到"能读到"的程度，不做批量导出。

## 常见坑

- 用扫描器输出直接下结论：命中模板不等于漏洞成立。
- 在没确认参数真的可控时就开始构造利用链，浪费时间且污染世界状态。
- 每条假设都不写证伪条件，导致被证伪后仍然反复回到同一条路。
- 长时间停留在单一技术栈：一类载荷连续失败三次以上就应当换族或换入口。

## 提示

- 用授权范围内的最小化动作推进，不做破坏性利用或持久化。
- 结论写完后追加到 `$HARNESS_EVENT_FILE`（`world.observe` / `world.hypothesis` / `world.artifact`）。
- 关键发现与排除路径记到 `$HARNESS_NOTES_DIR` 下的 markdown 笔记。
