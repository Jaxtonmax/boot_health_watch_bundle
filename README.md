# Boot Health Watch Bundle

这个目录是给板子 `root` 侧单独部署用的健康启动监控包。

它包含两层判定：

- `IRQ activity`
  - 由 `ivc_boot_watch` 监控 root 侧 IVC IRQ 是否有活动。
- `heartbeat/seq health`
  - 由 `root_guest_health_watch.py` 读取 `IVC_DEMO_BIN <id> receive` 的 `IVC_JSON` 输出，
    检查 `seq` 是否持续递增，并判断 guest Linux 与 guest RT-Thread 是否健康启动。
  - guest 进度条只会在对应 guest 真正启动后出现，不会在 root 刚开机时提前打印。

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
- `GUEST_LINUX_ZONE_CFG`
  - guest Linux zone 配置文件，默认 `/root/SeawayHyper/zone1/zone1-linux.json`。
- `GUEST_RTTHREAD_ZONE_CFG`
  - guest RT-Thread zone 配置文件，默认 `/root/SeawayHyper/zone2/zone2-rtt.json`。
- `GUEST_LINUX_DEVICE_PATH`
  - 可选，显式指定 guest Linux 监控设备，如 `/dev/hivc0`。
- `GUEST_RTTHREAD_DEVICE_PATH`
  - 可选，显式指定 guest RT-Thread 监控设备，如 `/dev/hivc2`。
- `GUEST_LINUX_IVC_ID`
  - legacy fallback。只有在没有 `DEVICE_PATH` 且无法通过映射解析时，才回退为 `/dev/hivc<id>`。
- `GUEST_RTTHREAD_IVC_ID`
  - legacy fallback。只有在没有 `DEVICE_PATH` 且无法通过映射解析时，才回退为 `/dev/hivc<id>`。
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
- `ROOT_BOOT_STAGE_BOOT_ID_FILE`
  - root 启动阶段日志的去重标记文件，默认 `/run/root_boot_health_watch.boot_id`。
  - 脚本会结合当前 Linux `boot_id`，保证 `Root Linux 启动成功` 在同一次 root 开机里只打印一次。

设备路径解析优先级是：

- `GUEST_*_DEVICE_PATH`
- `WEB_HVISOR_IVC_DEVICE_MAP`
- `WEB_HVISOR_IVC_DEVICE_MAP_BY_ID`
- `WEB_HVISOR_IVC_DEVICE_MAP_BY_OS`
- `GUEST_*_IVC_ID`

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
- 即使 service 在同一次开机中被 systemd 重启，`Root Linux 启动成功` 这类 root 启动阶段日志也只会在本次 root 开机里打印一次。
- guest 进度条只会在对应 zone 从 `not running -> running` 后出现；如果 watcher 启动时 guest 已经在运行，则不会补打一轮启动进度。

这里的“只打印一次”不是按 service 进程判断，而是按当前 Linux 的 `boot_id` 判断：

- 同一次 root 开机里，service 重启不会重复打印 root 启动成功；
- root 真正重启后，`boot_id` 会变化，启动日志会重新打印一次。

如果串口仍然没有看到启动信息，优先检查当前内核 `console=` 参数是否确实指向你正在看的串口。

正常情况下你会看到类似日志：

```text
[root-boot-health] Root Linux healthy boot stage reached
[root-boot-health] starting IRQ watcher: linux_irq=107 rtt_irq=109
[root-guest-health] [INFO] Guest Linux startup observation armed by zone start
[root-guest-health] [INFO] guest-progress[linux]: 0% waiting_zone 0s/30s
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

- `GUEST_LINUX_DEVICE_PATH`、`GUEST_RTTHREAD_DEVICE_PATH` 是否与实际通道一致；
- `GUEST_LINUX_ZONE_CFG`、`GUEST_RTTHREAD_ZONE_CFG` 是否指向正确 zone 配置；
- guest 是否确实在发送 `IVC_JSON`；
- `STARTUP_TIMEOUT_S` 是否过短。

3. root 开机后就开始刷 guest 进度条

现在的设计是：

- 只有当 guest 对应 zone 真正进入 `running` 后，才会开始打印该 guest 的进度条；
- 如果仍然在 root 开机后马上打印，优先检查 `GUEST_*_ZONE_ID` / `GUEST_*_ZONE_CFG` 是否配置错误，导致 watcher 误判 guest 已经启动。

## 手动验证

只验证 Linux 时：

```sh
systemctl restart root_boot_health_watch.service
journalctl -u root_boot_health_watch.service -f
```

然后手动启动 guest Linux，预期只会看到 `guest-progress[linux]`，不会提前看到 `guest-progress[rtt]`。

RT-Thread 现场验证建议步骤：

```sh
systemctl restart root_boot_health_watch.service
journalctl -u root_boot_health_watch.service -f
/root/SeawayHyper/bin/hvisor zone list
```

确认 RT-Thread 的 zone 还没运行后，再手动执行你平时的 RT-Thread 启动命令。预期行为是：

- root 开机后不出现 `guest-progress[rtt]`
- 手动启动 RT-Thread 后才开始出现 `guest-progress[rtt]`
- 随后进入 `waiting_device -> waiting_first_frame -> waiting_seq_updates -> startup_ok`

3. IRQ 有活动但 heartbeat 不通过

这通常说明：

- IVC 中断链路通了；
- 但 guest 应用层没有稳定输出 `seq` 递增帧；
- 这时应该优先排查 guest 发送程序或 `ivc_demo receive` 解析链路。
