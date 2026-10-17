# recon.active

domain: recon  
tags: recon, active, nmap, portscan

Active port scanning and service version detection on authorized targets.

## 何时使用

- `capability:network.reachability`（信任级别 ≥ evidence）

## 预期结论

- `observation:network.service`
- `entity:network.service`

## 侦察与判定信号

- 端口与服务：先小范围高频端口（常见 Web/SSH/RDP/数据库）建立轮廓，再按线索扩展；
  每个开放端口配一条服务与版本判定，而不是只留一个数字。
- 版本判定：banner、协议握手响应、TLS 证书主题与 SAN、HTTP 响应头。
  三者冲突时以最能复现的那个为准，并记下冲突本身。
- 拓扑线索：同一 IP 上的多个服务、反向代理与真实后端的差异、`robots.txt`/`sitemap.xml` 里的路径。
- 判定信号：一个新端口值得继续投入，是因为它暴露了新的攻击面（新服务、新版本、新入口），
  而不是因为"扫出来了"。

## payload 家族与变体

- 扫描族：全连接探测（`nmap -sT`、`nc -z`，可靠、慢）与半开探测（`nmap -sS`，快）二选一。
  半开探测、UDP 扫描与 `masscan` 都依赖原始套接字，只有 Agent 在配置里声明 `NET_RAW` 时才可用；
  未声明时这些命令会以 `Operation not permitted` 失败——换 connect 路径，不要原地重试。
- 服务识别族：协议探测脚本、版本指纹库、TLS 探测。先跑轻量指纹，再有选择地加脚本。
- 速率变体：按目标规模分级（单机/小网段/大网段），大网段要限速并分批，禁止全端口全速。
- 反模式：把全端口 `-p-` 当默认动作。缺省先扫常见端口，拿到线索再定向扩。

## 预期证据形态

- `observation:network.service`：host:port → 服务、版本、探测方式与时间。
- `entity:network.service`：作为后续假设主体的服务实体。
- 端口关闭/过滤同样是结论：写明"扫了但没开"，避免后面重复扫同一区间。

## 操作边界

- 只扫授权范围；范围以 CIDR/主机列表为准，不靠"看起来相关"扩边。
- 禁止 DoS 型参数（高并发、超大包、畸形包）；任何可能影响目标可用性的探测都要先降级。
- 扫描流量要与后续利用流量分开记录，便于解释异常。
- 探测到第三方托管（云、CDN、SaaS）时停下确认授权是否覆盖该托管面。

## 常见坑

- 有 WAF/IPS 时把"连接被重置"读成"端口关闭"，从而漏掉真实服务。
- 把识别软件包当侦察动作：求解期没有包源，装不上也来不及；用镜像已有的工具换路径。
- 只记录开放端口不记录服务版本，导致后面没有 payload 选择的依据。
- 把一次扫描结果当成恒定事实：目标会变，重要结论要带时间戳。
- 反复扫同一区间刷步数，却不产生新的服务事实——这属于重复动作，会被收敛机制打断。

## 提示

- 用授权范围内的最小化动作推进，不做破坏性利用或持久化。
- 结论写完后写入 ``；竞争会话可把摘要同步到 ``，不提交 typed World 事件。
- 关键发现与排除路径记到 `$HARNESS_NOTES_DIR` 下的 markdown 笔记。
