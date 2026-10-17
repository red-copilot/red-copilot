# recon.web-crawl

domain: web  
tags: recon, web, crawl, ffuf, gobuster

Spider and enumerate web endpoints, parameters, and hidden paths.

## 何时使用

- `entity:network.service`（信任级别 ≥ claim）

## 预期结论

- `entity:web.endpoint`
- `observation:web.endpoint`

## 侦察与判定信号

- 入口清单：页面链接、表单（method/action/字段名）、JS 里出现的接口路径、`robots.txt` 与站点地图、
  前端路由表。每个入口记录"谁调用它、需要什么身份"。
- 参数面：查询串、表单字段、JSON 字段、HTTP 头（`Referer`、`X-Forwarded-*`）、Cookie。
  参数是漏洞假设的载体，发现新参数比发现新页面更有价值。
- 身份面：是否需要登录、会话如何传递、是否存在注册/找回/邀请等自助入口。
- 判定信号：一个接口值得跟进，是因为它的输入会到达服务端并有副作用（读写、状态变更），
  而不是因为路径名字看起来敏感。

## payload 家族与变体

- 抓取族：HTML 链接与表单解析、JS 静态分析取接口、站点地图/爬虫清单、历史路径猜测（低优先）。
- 变体：同一路径的不同方法（GET/POST/PUT/DELETE）、不同 `Content-Type`（表单 vs JSON）、
  不同身份（匿名/低权限/高权限）分别记录，它们经常给出不同结果。
- 反模式：把爬取当扫描。爬取只负责"知道有哪些入口"，不负责验证漏洞。
- 枚举参数：`ffuf -w <字典> -u <url>/FUZZ -mc 200,204,301,302,401,403 -t 40 -rate 200`
  起步，先单层、再用 `-recursion -recursion-depth 2`；`gobuster dir -t 40 -x php,txt,bak`
  是等价备选。速率要按目标规模定，别用默认值对着小站全速打。
- 断点与回退：字典缺失或工具报错时，用 `curl` 打已知路径确认目标仍在响应，再换
  `gobuster`/`dirb` 或手工路径清单继续；扫描器不可用不是放弃侦察的理由，只是换一条路径。

## 预期证据形态

- `observation:web.endpoint`：method + path + 参数名 + 是否需要身份。
- `entity:web.endpoint`：作为后续漏洞假设主体的接口实体。
- 用一份可复现的最小请求（方法、路径、必要头、示例参数）记录入口；不要只写页面标题。

## 操作边界

- 抓取深度与并发要有上限；对同一站点的请求间隔要能说明；不跟随站外链接到授权范围之外。
- 不触发破坏性状态变更（删除、支付、批量群发）；发现这类入口只记录，不测试。
- 上传、导入、订阅等会写入数据的入口，只记录形态，是否测试由范围与预算决定。

## 常见坑

- 只爬静态 HTML，漏掉单页应用的接口（接口都在 JS 与 XHR 里）。
- 把前端路由当成服务端入口，后面所有假设都建立在错误前提上。
- 忽略同路径不同方法/不同身份的差异，导致"这个接口很普通"的错误结论。
- 入口清单不收尾：爬到几百条却没有任何一条进入验证阶段，属于只侦察不推进。

## 提示

- 用授权范围内的最小化动作推进，不做破坏性利用或持久化。
- 结论写完后追加到 `$HARNESS_EVENT_FILE`（`world.observe` / `world.hypothesis` / `world.artifact`）。
- 关键发现与排除路径记到 `$HARNESS_NOTES_DIR` 下的 markdown 笔记。
