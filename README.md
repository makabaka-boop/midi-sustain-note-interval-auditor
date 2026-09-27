# sustain — 钢琴事件流延音审计器

只接收 JSON 的命令行审计器：从 stdin 读取一份事件流，输出每个音符的按键区间、
实际发声区间（含结束原因）以及全局峰值同时发声数。松键不等于停止发声——踏板
踩下时音符延续，同音高重复按下会形成多个待配对的音符。

## 运行

Docker Compose（一次性 CLI，`-T` 关闭伪终端以便管道输入）：

```bash
docker compose run --rm -T sustain < examples/input.json
```

本地（Python 3.9+，仅标准库）：

```bash
python -m sustain < examples/input.json
```

退出码：`0` 成功（结果 JSON 写 stdout）；`1` 整份拒绝（错误 JSON 写 stderr）。

## 输入

```json
{
  "endTime": 90,
  "events": [
    {"time": 10, "type": "NOTE_ON",    "channel": 0, "pitch": 60},
    {"time": 20, "type": "PEDAL_DOWN", "channel": 0},
    {"time": 30, "type": "NOTE_ON",    "channel": 0, "pitch": 64},
    {"time": 40, "type": "NOTE_OFF",   "channel": 0, "pitch": 60},
    {"time": 50, "type": "PEDAL_UP",   "channel": 0}
  ]
}
```

* `events`：1～5000 个事件，`time` 非负且按非降序排列；**同一时刻的事件按输入
  顺序执行**。
* `type`：`NOTE_ON` / `NOTE_OFF`（需要 `pitch` 0～127）、`PEDAL_DOWN` /
  `PEDAL_UP`（每通道独立，无需 `pitch`）。`channel` 为 0～15。
* `endTime`：不早于末事件时间；仍在发声的音符在此截断。

## 语义

* `NOTE_ON` 新开一个音符；同音高重复按下会堆叠成多个待配对音符。
* `NOTE_OFF` 释放该通道同音高**最早仍按住**的 `NOTE_ON`（FIFO 配对）。
  踏板踩下时只松键、不结束发声；未踩踏板则发声立即结束。
* `PEDAL_UP` 结束该通道全部已松键的音符；仍按住的音符继续发声。
* 到 `endTime` 仍发声的音符被截断并标记 `END_TIME`；仍按住的琴键其按键区间
  也在 `endTime` 截断。
* 峰值同时发声数按半开区间 `[soundStart, soundEnd)` 统计：一个音符在 t 结束、
  另一个在 t 开始不算重叠；零长度音符不贡献峰值。

## 输出

```json
{
  "endTime": 90,
  "notes": [
    {"id": 0, "channel": 0, "pitch": 60,
     "keyStart": 10, "keyEnd": 40,
     "soundStart": 10, "soundEnd": 50, "endReason": "PEDAL_UP"},
    {"id": 1, "channel": 0, "pitch": 64,
     "keyStart": 30, "keyEnd": 90,
     "soundStart": 30, "soundEnd": 90, "endReason": "END_TIME"}
  ],
  "peakPolyphony": 2
}
```

* `notes`：按 `NOTE_ON` 出现顺序排列，`id` 从 0 递增。
* `keyStart`/`keyEnd`：按键区间；`soundStart`/`soundEnd`：实际发声区间。
* `endReason`：`NOTE_OFF`（松键即停）、`PEDAL_UP`（抬踏板终止）、
  `END_TIME`（在 endTime 截断）。
* `peakPolyphony`：全局峰值同时发声数（跨通道）。

## 拒绝条件（整份拒绝，退出码 1）

| 错误码 | 条件 |
|---|---|
| `INVALID_JSON` | stdin 不是合法 JSON |
| `BAD_PAYLOAD` | 顶层非对象、`events` 数量不在 1～5000、`endTime` 缺失/非法/早于末事件 |
| `BAD_EVENT` | 事件非对象、类型未知、时间/通道/音高越界或缺失 |
| `TIME_ORDER` | 事件时间倒退 |
| `ORPHAN_NOTE_OFF` | `NOTE_OFF` 找不到仍按住的同通道同音高 `NOTE_ON` |
| `REPEATED_PEDAL` | 踏板已踩又 `PEDAL_DOWN`，或已抬又 `PEDAL_UP`（按通道判定） |

错误输出形如 `{"error": {"code": "ORPHAN_NOTE_OFF", "message": "...", "eventIndex": 3}}`。

## 测试

```bash
pip install pytest
pytest
```

测试以逐音符对象模型整体对拍，覆盖：同音重叠（FIFO 配对、延音中重敲）、
跨通道（踏板与音符互不影响）、同刻抬踏板（输入序决定 `endReason`、新音符
不被同刻抬踏板终止）、峰值统计边界、全部拒绝路径与 1/5000/5001 事件边界，
以及 CLI 子进程级用例。

## 结构

```
sustain/core.py   校验 + 状态机 + 峰值扫描
sustain/cli.py    stdin/stdout、退出码
tests/            pytest 套件
Dockerfile        python:3.12-slim，ENTRYPOINT python -m sustain
docker-compose.yml  sustain 服务（一次性 CLI）
```
