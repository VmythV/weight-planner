# TODOLIST

## 一、部署到 Mac mini（第 1 阶段收尾）

- [ ] 把 `weight-planner` 目录拷到 Mac mini，运行 `deploy/install-launchd.sh`
- [ ] Mac mini：系统设置 → 能源 → 打开"显示器关闭时防止自动进入睡眠"
- [ ] Mac mini：系统设置 → 用户与群组 → 打开自动登录（停电重启后服务才能自动起来）
- [ ] 路由器里给 Mac mini 做 DHCP 地址保留（固定 IP），第 2 阶段 ESP32 要用 IP 访问
- [ ] 手机浏览器打开页面，"添加到主屏幕"
- [ ] 在「计划设置」里填写个人信息和目标
- [ ] 从米家 App 里抄几个历史关键日期的体重，用「批量导入」补进去（米家无法导出历史数据）
- [ ] 给 `data/weight.db` 安排备份（Time Machine，或定时拷贝到网盘）

## 二、蓝牙自动采集（第 2 阶段）

已确认的设备信息：

| 项目 | 值 |
|---|---|
| 型号 | 米家体脂秤 S400 Pro（MJTZC03YM） |
| 蓝牙产品 ID | `0x4B05`（nRF Connect 实测） |
| MAC 地址 | 见 ESP32 配置 |
| 广播加密 | 是，需要 bindkey |

- [ ] 买一块带蓝牙的 ESP32 开发板（ESP32 / ESP32-C3 / ESP32-S3 均可），用 USB 充电头供电，放在秤附近
- [ ] 获取 bindkey：用 [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) 在本机运行一次，服务器区域选 `cn`，记下这台秤的 BLE KEY（32 位十六进制）
  - 自己在终端输入账号密码，不要交给别人
  - 每天的短信/邮件验证码有 3–5 次上限
  - 以后在米家里重置或重新绑定秤，bindkey 会变，需要重新获取
- [ ] 等 ESPHome 2026.10.0 正式版发布（`xiaomi_body_scale` 组件目前只在 2026.10.0 测试版里）
- [ ] 编写 ESPHome 配置：
  - `xiaomi_body_scale`：填 MAC 和 bindkey，启用 weight / impedance_low / impedance_high / heart_rate / stabilized
  - `wifi` + `http_request`：`stabilized` 变为 true 时，POST 到 `http://<Mac mini IP>:8765/api/weighins`，`source` 填 `esp32`
  - 没测到阻抗或心率时（例如穿袜子称）不要发送 NaN，字段直接省略
- [ ] 实测验证：
  - S400 Pro 能否正常解密（开源库里的 `0x4B05` 是照搬别的项目的设备表，还没见到真机测试记录）
  - ESP32 上报的体重与米家显示一致
  - 体重包和阻抗包的到达间隔在 2 分钟合并窗口以内（见 `app/db.py` 的 `MERGE_WINDOW`）
- [ ] 决定 Mac mini 离线时 ESP32 上报失败怎么处理（重试几次，或者接受丢失这一次）
- [ ] ESP32 接入后，考虑给写入接口加一个简单的 token 校验

## 三、功能增强（第 3 阶段，可选）

- [ ] 每日推送：每天早上推送"今天可摄入 / 需消耗 / 当前状态"（可选 ntfy、企业微信机器人、Server 酱）
- [ ] 饮食记录：记录每日实际摄入，并用"平均摄入 − 趋势体重变化 × 7700 / 天数"反推真实消耗，替代公式估算（自适应消耗）
- [ ] 计划历史：目前修改「计划设置」会覆盖原计划，改为保留历次计划，在图上能对比
- [ ] 体脂率、肌肉量估算：基于 S400 Pro 上报的双频阻抗；数值会和米家不一致，只看趋势
- [ ] PWA：加 manifest 和图标，添加到主屏幕后像独立 App 一样打开
- [ ] 数据导出 CSV
- [ ] 需要外网访问时：加登录验证，或用 Tailscale 之类的组网工具，不直接暴露端口

## 四、算法待评估

- [ ] 7700 kcal/kg 是近似值，减重初期掉的多是水分和糖原；可评估是否引入 NIH（Kevin Hall）的动态体重模型
- [ ] 活动系数是主观选择的，没有饮食记录时无法校准真实消耗（依赖第三部分的"饮食记录"）
- [ ] 用一两个月的真实数据回看趋势体重的平滑参数（21 天窗口、10 天半衰期）是否合适
