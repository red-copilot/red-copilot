# recon.passive

domain: recon  
tags: recon, passive, osint, dns, whois

Passive reconnaissance — WHOIS, DNS, certificate transparency, and technology fingerprinting.

## 何时使用

- `capability:network.reachability`（信任级别 ≥ evidence）

## 预期结论

- `observation:recon.dns`
- `observation:recon.tech-stack`
- `entity:network.host`

## 侦察与判定信号

- 资产归属链：域名 → DNS 记录 → 组织/ASN。只有当"这个名字确实指向授权范围内的一台主机"成立时，
  才把它记成资产；孤立的字符串（邮件地址、水印、页面里的示例域名）不是资产。
- 域名与子域：whois 注册信息、A/AAAA/CNAME/MX/TXT 记录、证书透明度日志、被动 DNS 数据。
  同一实体的多个名字（`www` / `api` / 别名 CNAME）分别记录，不要合并。
- 技术栈指纹：响应头（`Server`、`X-Powered-By`、框架 Cookie 名）、favicon 哈希、静态资源路径与版本串、
  默认错误页模板。指纹的用途是缩小后续 payload 选择范围，不是结论本身。
- 命名空间线索：端口/路径命名习惯、页面里出现的内部域名、公开文档与代码库中泄露的资产名。
  这些只是"下一个要查什么"的输入。

## payload 家族与变体

- 成本从低到高：本地解析数据 < 第三方被动数据源 < 主动 DNS 查询 < 对目标直接发包。
  先用完前三层，再去动第四层。
- 查询族：whois（注册与网段）、DNS（记录类型）、证书透明度（子域历史）、公开枚举接口（子域字典命中）。
- 变体：通配符 DNS 必须识别（随机子域也解析成功说明有泛解析，不能用"能解析"当存在证据）。
- 反模式：用大字典硬刷子域。被动阶段的目标是拿到"值得主动验证的短名单"，不是穷举。

## 预期证据形态

- `observation:recon.dns`：域名 → 解析结果，附查询来源与时间。
- `observation:recon.tech-stack`：指纹片段与其出处（响应头原文、资源路径）。
- `entity:network.host`：通过归属链确认的主机实体。
- 每条观察写清"我是怎么知道的"。第三方数据源与本地解析的置信度不同，必须能区分。

## 操作边界

- 只对授权范围内、且能建立归属链的资产做被动收集；范围外的名字不查、不记、不试探。
- 被动阶段不应向目标发送任何业务流量。DNS 查询要遵守上游限速，不并发轰炸解析器。
- 收集到的任何凭据、Cookie、个人信息都不进笔记与工件；需要引用时只记位置，不记明文。

## 常见坑

- 把 CDN/WAF 的地址当成目标主机：先看证书与响应特征，确认你打的是资产还是前置。
- 泛解析导致资产清单虚高，后续扫描全部打在同一个前置上。
- 只记结论不记来源，导致后面的假设无法回溯，也无法向平台解释你为什么这么打。
- 过早进入主动扫描：被动情报没收敛就动手，噪声大、覆盖差、还容易触发防护。

## 提示

- 用授权范围内的最小化动作推进，不做破坏性利用或持久化。
- 结论写完后追加到 `$HARNESS_EVENT_FILE`（`world.observe` / `world.hypothesis` / `world.artifact`）。
- 关键发现与排除路径记到 `$HARNESS_NOTES_DIR` 下的 markdown 笔记。
