# Boot Health Watch Bundle

这个目录是给板子 `root` 侧单独部署用的健康启动监控包。

它包含两层判定：

- `IRQ activity`
  - 由 `ivc_boot_watch` 监控 root 侧 IVC IRQ 是否有活动。
- `heartbeat/seq health`
  - 由 `root_guest_health_watch.py` 读取 `IVC_DEMO_BIN <id> receive` 的 `IVC_JSON` 输出，
    检查 `seq` 是否持续递增，并判断 guest Linux 与 guest RT-Thread 是否健康启动。

只有 heartbeat 侧才是最终健康判定；IRQ 侧是辅助链路活性判断。

## 目录内容

- `ivc_boot_watch`
  - IRQ 活动监控二进制。
- `ivc_boot_watch.c`
  - IRQ 活动监控源码。
- `root_boot_health_watch.sh`
  - 总入口脚本，负责先打印 root 健康阶段，再拉起 IRQ watcher 和 heartbeat watcher。
- `root_guest_health_watch.py`
  - heartbeat/seq 健康判定程序。
- `root_boot_health_watch.service`
  - `systemd` 服务文件。
- `root_boot_health_watch.env.example`
  - 配置样例。
- `install.sh`
  - 一键安装脚本。

## 快速安装

在板子 `root` 上执行：

```sh
cd /root/boot_health_watch_bundle
./install.sh
```

如果你只想安装文件，不立刻启动服务：

```sh
cd /root/boot_health_watch_bundle
NO_START=1 ./install.sh
```

安装后会把文件放到：

- `/usr/local/sbin/ivc_boot_watch`
- `/usr/local/sbin/root_boot_health_watch.sh`
- `/usr/local/sbin/root_guest_health_watch.py`
- `/etc/systemd/system/root_boot_health_watch.service`
- `/etc/default/root_boot_health_watch`
- `/etc/default/root_boot_health_watch.example`

## 配置方法

编辑：

```sh
vi /etc/default/root_boot_health_watch
```

重点参数：

- `GUEST_LINUX_IRQ`
  - guest Linux 对应的 root 侧 IRQ 号。
- `GUEST_RTTHREAD_IRQ`
  - guest RT-Thread 对应的 root 侧 IRQ 号。
- `GUEST_LINUX_IVC_ID`
  - guest Linux 使用的 IVC 设备号，默认 `0`。
- `GUEST_RTTHREAD_IVC_ID`
  - guest RT-Thread 使用的 IVC 设备号，默认 `2`。
- `IVC_DEMO_BIN`
  - 接收程序的路径，推荐设置为 `/usr/local/bin/ivc_monitor`。
- `STARTUP_TIMEOUT_S`
  - 启动窗口，默认 `30` 秒。
- `HEARTBEAT_TIMEOUT_S`
  - 心跳超时，默认 `15` 秒。
- `MIN_SEQ_UPDATES`
  - 启动成功需要观察到的 `seq` 递增次数，默认 `2`。
- `ENABLE_IRQ_WATCH`
  - 是否启用 IRQ watcher，默认 `1`。

按你现在 guest 侧的发送逻辑，默认值的含义是：

- guest 在 `30s` 内至少要有 `2` 次 `seq` 递增；
- 任一 guest 超过 `15s` 没有新帧，就判定 heartbeat 超时。

## 启动与查看日志

启动或重启服务：

```sh
systemctl daemon-reload
systemctl restart root_boot_health_watch.service
systemctl status root_boot_health_watch.service
```

查看实时日志：

```sh
journalctl -u root_boot_health_watch.service -f
```

这个 service 现在的行为是：

- service 标准输出只写入 `journal`
- 只有入口脚本的少量启动信息会额外写入 `/dev/console`

所以正常情况下：

- 启动早期你会在串口看到几条关键信息；
- 后续持续监控日志只在 `journalctl` 中查看，不会持续占用串口影响输入。
- 即使 service 在同一次开机中被 systemd 重启，串口启动日志也只会打印一次。

如果串口仍然没有看到启动信息，优先检查当前内核 `console=` 参数是否确实指向你正在看的串口。

正常情况下你会看到类似日志：

```text
[root-boot-health] Root Linux healthy boot stage reached
[root-boot-health] starting IRQ watcher: linux_irq=107 rtt_irq=109
[root-guest-health] [INFO] Guest Linux first frame received (seq=1)
[root-guest-health] [INFO] RT-Thread first frame received (seq=1)
[root-guest-health] [INFO] Guest Linux startup healthy: seq updated 2 times (seq=3)
[root-guest-health] [INFO] RT-Thread startup healthy: seq updated 2 times (seq=3)
[root-guest-health] [INFO] healthy boot confirmed for all guests
```

## 判定逻辑

启动成功条件：

- root 脚本已经启动；
- guest Linux 在启动窗口内收到首帧并满足 `MIN_SEQ_UPDATES` 次 `seq` 递增；
- guest RT-Thread 在启动窗口内收到首帧并满足 `MIN_SEQ_UPDATES` 次 `seq` 递增。

运行中失败条件：

- 任一 guest 在 `HEARTBEAT_TIMEOUT_S` 内没有新帧；
- 或 watcher 进程退出并在恢复前持续超时。

## 常见问题

1. 日志提示 `ivc receiver not found`

检查：

```sh
ls -l /usr/local/bin/ivc_monitor
```

或者把 `IVC_DEMO_BIN` 改成实际路径。

2. 日志提示 startup timeout

检查：

- `GUEST_LINUX_IVC_ID`、`GUEST_RTTHREAD_IVC_ID` 是否与实际通道一致；
- guest 是否确实在发送 `IVC_JSON`；
- `STARTUP_TIMEOUT_S` 是否过短。

3. IRQ 有活动但 heartbeat 不通过

这通常说明：

- IVC 中断链路通了；
- 但 guest 应用层没有稳定输出 `seq` 递增帧；
- 这时应该优先排查 guest 发送程序或 `ivc_demo receive` 解析链路。
