# DREAMS PV Logger MQTT 技術規格書

版本：1.0  
日期：2026-05-28  
適用對象：PV Gateway / DataLogger 開發者、DREAMS Outstation 維運者

## 1. 目的與範圍

本文件定義 PV Logger 與 DREAMS Outstation 之間的 MQTT 介面。PV Logger 只需要依照本文件發布量測資料、狀態、事件與指令回覆；DREAMS Outstation 會將資料轉成 DNP3 Outstation 點值，提供 DREAMS Master / 台電端讀取與控制。

本文件重點：

- MQTT broker 連線設定
- MQTT topic 命名規則
- `snapshot`、`event`、`status`、`cmd`、`cmd_ack` payload 格式
- AI/AO 點位對應
- DNP3 ID 與 `logger_id` 綁定邏輯
- PV Logger 實作注意事項

## 2. 系統角色

```text
DREAMS Master / 台電端
        |
        | DNP3 TCP 20000
        v
DREAMS Outstation
        |
        | MQTT DREAMS/{logger_id}/{suffix}
        v
MQTT Broker
        |
        v
PV Gateway / DataLogger
```

| 角色 | 說明 |
| --- | --- |
| PV Logger | 發布即時點值、事件、狀態；接收 Outstation 發出的控制命令 |
| MQTT Broker | PV Logger 與 Outstation 的訊息交換中心 |
| DREAMS Outstation | 訂閱 MQTT，轉換成 DNP3 Outstation 點值；接收 DNP3 AO 後轉成 MQTT `cmd` |
| DREAMS Master Simulator | 本專案提供的台電端 DNP3 Master 模擬器 |

## 3. MQTT 連線設定

| 項目 | 測試/目前設定 |
| --- | --- |
| Protocol | MQTT 3.1.1 / TCP |
| Broker host | `34.80.23.92` |
| Broker port | `1883` |
| Username | `dev` |
| Password | 由專案負責人安全提供，不建議寫入規格書或程式碼 |
| Root topic | `DREAMS` |
| QoS | `1` |
| Retain | 建議 `false` |
| Payload encoding | UTF-8 JSON object |

PV Logger 必須處理 QoS 1 可能造成的重複訊息。收到 `cmd` 時應以 `cmd_id` 做去重，避免同一指令重複執行。

## 4. Topic 命名規則

正式格式：

```text
DREAMS/{logger_id}/{suffix}
```

範例：

```text
DREAMS/logger_test02/snapshot
DREAMS/logger_test02/event
DREAMS/logger_test02/status
DREAMS/logger_test02/cmd
DREAMS/logger_test02/cmd_ack
```

`logger_id` 規則：

- 每台 PV Logger 必須有唯一且穩定的 `logger_id`。
- 不使用 `site_id` 作為 MQTT 維度。
- DNP3 ID 綁定主體是 `logger_id -> dnp3Address`。
- 不支援 `DREAMS/{site_id}/{logger_id}/{suffix}`。
- Topic 必須剛好三層，額外層級會被 Outstation 忽略。

## 5. Topic 方向

| Topic | 方向 | 用途 |
| --- | --- | --- |
| `DREAMS/{logger_id}/snapshot` | PV Logger -> Outstation | 完整點值快照 |
| `DREAMS/{logger_id}/event` | PV Logger -> Outstation | deadband / 變動點事件 |
| `DREAMS/{logger_id}/status` | PV Logger -> Outstation | Logger 上下線狀態 |
| `DREAMS/{logger_id}/cmd` | Outstation -> PV Logger | 控制命令 |
| `DREAMS/{logger_id}/cmd_ack` | PV Logger -> Outstation | 控制命令執行回覆 |

Outstation 會訂閱：

```text
DREAMS/+/+
```

## 6. 通用 Payload 規則

所有 MQTT payload 必須是 JSON object。

通用欄位：

| 欄位 | 型別 | 必填 | 說明 |
| --- | --- | --- | --- |
| `ts` | integer | 建議必填 | Unix timestamp，單位秒 |
| `data` | object | 視 topic | AI 點值資料 |
| `reason` | string | 選填 | 訊息原因，例如 `startup`、`periodic`、`deadband` |

時間規則：

- `ts` 使用 Unix seconds。
- 時區顯示以 `Asia/Taipei` 為主，但 MQTT payload 不要送本地時間字串作為主時間。
- `AI_32` 是 Timestamp 點，Outstation 會以 payload `ts` 更新，PV Logger 不需要在 `data` 內送 `AI_32`。

數值規則：

- `data` 內 AI 值請送工程值，不要送 DNP3 raw value。
- Outstation 會依 AI 點表自動轉成 DNP3 raw value。
- 例如 `AI_4 = 380.1` 代表 380.1 V，Outstation 會轉成 DNP raw `38010`。

## 7. Snapshot

Topic：

```text
DREAMS/{logger_id}/snapshot
```

用途：

- 回報完整目前點值。
- PV Logger 啟動、重連或 15 分鐘週期資料應使用 `snapshot`。
- Outstation 收到 `reason=startup` 或 `reason=periodic` 時，會立即轉成 DNP3 periodic snapshot event。

建議發送時機：

- Logger 啟動後立即送一次：`reason=startup`
- 每 15 分鐘送一次：`reason=periodic`
- 建議對齊 00、15、30、45 分鐘
- MQTT reconnect 後建議再送一次 `startup` 或完整 snapshot

Payload 範例：

```json
{
  "ts": 1779955200,
  "reason": "periodic",
  "data": {
    "AI_0": 144.0,
    "AI_1": 144.0,
    "AI_2": 144.0,
    "AI_3": 0.0,
    "AI_4": 23000.0,
    "AI_5": 23000.0,
    "AI_6": 23000.0,
    "AI_7": 52000,
    "AI_8": 1200,
    "AI_9": 99.5,
    "AI_10": 60.0,
    "AI_11": 2147480000,
    "AI_12": 850,
    "AI_13": 2.1,
    "AI_14": 100,
    "AI_15": 100,
    "AI_16": 0,
    "AI_17": 105,
    "AI_18": 0,
    "AI_19": 0,
    "AI_20": 0.5,
    "AI_21": 0.5,
    "AI_22": 0.5,
    "AI_23": 0.5,
    "AI_24": 0.5,
    "AI_25": 0.5,
    "AI_26": 0.5,
    "AI_27": 1.0,
    "AI_28": 1.0,
    "AI_29": 1.0,
    "AI_30": 0.5
  }
}
```

注意：

- `snapshot` 建議帶完整 AI 點值。
- `AI_31` 預設停用，不要送。
- `AI_32` 由 `ts` 自動產生，不要送。

## 8. Event / Deadband

Topic：

```text
DREAMS/{logger_id}/event
```

用途：

- 回報 deadband 觸發或短時間內變動的點。
- 只需要送變動的 AI 點，不要送完整快照。
- Outstation 會將可作為事件的 AI 轉成 DNP3 non-periodic event。

Payload 範例：

```json
{
  "ts": 1779955261,
  "reason": "deadband",
  "data": {
    "AI_7": 53120,
    "AI_8": 1300
  }
}
```

Event 可送的點：

- 主要量測點：`AI_0` 到 `AI_10`
- 控制回饋點：`AI_14` 到 `AI_30`

Event 不建議送的點：

- `AI_11` Accumulated Energy：snapshot 會帶，但 deadband event 不會轉成 DNP3 event
- `AI_12` Irradiance：snapshot 會帶，但 deadband event 不會轉成 DNP3 event
- `AI_13` Wind Speed：snapshot 會帶，但 deadband event 不會轉成 DNP3 event
- `AI_32` Timestamp：由 `ts` 控制

## 9. Status

Topic：

```text
DREAMS/{logger_id}/status
```

用途：

- 告知 Outstation 該 Logger 目前 online / offline。
- Outstation UI 會顯示最後狀態與時間。

Payload 範例：

```json
{
  "ts": 1779955200,
  "status": "online",
  "firmware": "1.2.3",
  "ip": "192.168.1.50"
}
```

支援狀態：

| `status` | 說明 |
| --- | --- |
| `online` | Logger 在線 |
| `offline` | Logger 離線 |

其他欄位如 `firmware`、`ip`、`rssi`、`message` 可作為診斷資訊，Outstation 目前只用 `status` 判斷 online/offline。

## 10. Command

Topic：

```text
DREAMS/{logger_id}/cmd
```

方向：

```text
Outstation -> PV Logger
```

用途：

- DREAMS Master 下 DNP3 AO command 後，Outstation 轉成 MQTT `cmd` 發給指定 logger。
- Outstation UI 手動命令也會發布到同一個 topic。

Payload 範例：

```json
{
  "cmd_id": "4d6b9c67-7a40-4f64-90cb-c0d8b0c777a5",
  "ts": 1779955300,
  "type": "control",
  "target": "active_power_percent",
  "value": 50,
  "unit": "%",
  "raw_ao_index": 1,
  "raw_value": 50
}
```

欄位說明：

| 欄位 | 型別 | 必填 | 說明 |
| --- | --- | --- | --- |
| `cmd_id` | string | 是 | 指令唯一 ID，PV Logger 回覆 `cmd_ack` 必須帶回 |
| `ts` | integer | 是 | Outstation 發送命令時間 |
| `type` | string | 是 | 命令類型，只會是 `control` 或 `config_deadband`；用法見下表 |
| `target` | string | 是 | 控制或設定目標 |
| `value` | number | 是 | 工程值 |
| `unit` | string | 是 | 工程單位 |
| `raw_ao_index` | integer | 是 | DNP3 AO index |
| `raw_value` | number | 是 | DNP3 AO command 原始值 |
| `inverter_index` | integer | 選填 | Outstation UI 手動命令可指定 1 到 50 |
| `source` | string | 選填 | 例如 `dreams-outstation-ui` |

`type` 判斷規則：

| `type` | 何時使用 | 對應 DNP3 AO | 對應 `target` |
| --- | --- | --- | --- |
| `control` | 逆變器控制或控制模式設定 | `AO_0` 到 `AO_4` | `pf_percent`、`active_power_percent`、`reactive_power_percent`、`vpset`、`autonomous_control` |
| `config_deadband` | 設定 AI deadband 門檻值 | `AO_5` 到 `AO_15` | `Deadband_AI_0` 到 `Deadband_AI_10` |

PV Logger 應以 `cmd.type` 判斷處理流程：

- `type="control"`：執行逆變器控制，例如有效功率、功率因數、電壓設定。
- `type="config_deadband"`：更新本機 AI deadband 設定，不是逆變器出力控制。
- 不建議只靠 `target` 字串猜測命令類型；`type` 是 Outstation 依 DNP3 AO index 產生的正式分類。

PV Logger 收到 `cmd` 後應：

1. 檢查 `cmd_id` 是否已處理過，避免 QoS 1 重複執行。
2. 依 `type`、`target`、`value` 執行控制或設定。
3. 執行完成後發布 `cmd_ack`。
4. 若命令不支援，回覆 `status=FAILED` 並提供 `message`。

## 11. Command Ack

Topic：

```text
DREAMS/{logger_id}/cmd_ack
```

方向：

```text
PV Logger -> Outstation
```

用途：

- 回覆 `cmd` 是否執行成功。
- Outstation 收到 `SUCCESS` 後，會將控制成功狀態轉成 DNP3 event：
  - `AI_18`
  - `AI_19`
  - 對應 AO feedback AI

成功範例：

```json
{
  "ts": 1779955310,
  "cmd_id": "4d6b9c67-7a40-4f64-90cb-c0d8b0c777a5",
  "status": "SUCCESS",
  "inverter_index": 1,
  "raw_ao_index": 1,
  "target": "active_power_percent",
  "value": 50,
  "message": "active power command applied"
}
```

失敗範例：

```json
{
  "ts": 1779955310,
  "cmd_id": "4d6b9c67-7a40-4f64-90cb-c0d8b0c777a5",
  "status": "FAILED",
  "inverter_index": 1,
  "raw_ao_index": 1,
  "target": "active_power_percent",
  "value": 50,
  "error_code": "INVERTER_OFFLINE",
  "message": "inverter 1 is offline"
}
```

欄位說明：

| 欄位 | 型別 | 必填 | 說明 |
| --- | --- | --- | --- |
| `cmd_id` | string | 是 | 必須與收到的 `cmd_id` 相同 |
| `status` | string | 是 | `SUCCESS` 或 `FAILED` |
| `ts` | integer | 建議必填 | 執行完成時間 |
| `inverter_index` | integer | 建議必填 | 成功/失敗的 inverter 編號，1 到 50；未提供時 Outstation 預設為 1 |
| `raw_ao_index` | integer | 建議 | 原命令 AO index |
| `target` | string | 建議 | 原命令 target |
| `value` | number | 建議 | 實際套用工程值 |
| `error_code` | string | 失敗時建議 | 失敗原因代碼 |
| `message` | string | 建議 | 人可讀訊息 |

目前版本限制：

- Outstation 目前以一個 `cmd_id` 對應一筆 `cmd_ack`。
- 收到 `SUCCESS` 才會更新 DNP3 command feedback event。
- 收到 `FAILED` 會記錄狀態，但不會送 AI_18 / AI_19 成功 bit。
- 若未來需要完整支援「部分成功、部分失敗」的多 inverter aggregate 結果，需要擴充 `cmd_ack` schema 與 Outstation 處理邏輯。

## 12. DREAMS Master 指令範例

PV Logger 不直接接觸 DNP3。DREAMS Master 對 Outstation 下 DNP3 指令後，只有 Analog Output control 會被 Outstation 轉成 MQTT `cmd` 發給 PV Logger。

Master Poll 類指令不會發 MQTT `cmd`：

| DREAMS Master 操作 | DNP3 行為 | PV Logger 是否收到 MQTT `cmd` |
| --- | --- | --- |
| Read AI | 讀取 Static Analog Input | 否 |
| Scan Events | 讀取 Class 1/2/3 event | 否 |
| Enable Unsolicited | 啟用 Outstation 主動上報 event | 否 |
| Analog Output | 寫入 AO command | 是 |

目前 Master Simulator / Master UI 的 AO 操作固定採用 DREAMS 控制流程：

| 項目 | 值 |
| --- | --- |
| DNP3 Object | Object 41 |
| Variation | Variation 2, 16-bit Analog Output Block |
| Function | Function 5, Direct Operate |
| Command target | 單一 DNP3 ID |

### 12.1 Master Poll 範例

讀 AI_0 到 AI_32：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
.venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py \
  --host 127.0.0.1 --port 20000 \
  --master-address 100 --outstation-address 520 \
  range 0 32
```

掃描 event：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
.venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py \
  --host 127.0.0.1 --port 20000 \
  --master-address 100 --outstation-address 520 \
  scan --classes events
```

常駐監看 unsolicited event：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
.venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py \
  --host 127.0.0.1 --port 20000 \
  --master-address 100 --outstation-address 520 \
  monitor --enable-unsolicited
```

以上三種操作只會讀 Outstation 目前已有的 DNP3 點值或等待 Outstation 上報，不會發布 `DREAMS/{logger_id}/cmd`。

### 12.2 AO_1 Active Power Setpoint = 50%

DREAMS Master Simulator：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
.venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py \
  --host 127.0.0.1 --port 20000 \
  --master-address 100 --outstation-address 520 \
  ao 1 50
```

Outstation 發給 PV Logger：

```text
Topic: DREAMS/logger_test02/cmd
```

```json
{
  "cmd_id": "4d6b9c67-7a40-4f64-90cb-c0d8b0c777a5",
  "ts": 1779955300,
  "type": "control",
  "target": "active_power_percent",
  "value": 50,
  "unit": "%",
  "raw_ao_index": 1,
  "raw_value": 50
}
```

PV Logger 成功後回覆：

```text
Topic: DREAMS/logger_test02/cmd_ack
```

```json
{
  "ts": 1779955310,
  "cmd_id": "4d6b9c67-7a40-4f64-90cb-c0d8b0c777a5",
  "status": "SUCCESS",
  "inverter_index": 1,
  "raw_ao_index": 1,
  "target": "active_power_percent",
  "value": 50,
  "message": "active power command applied"
}
```

Outstation 轉回 DNP3 event：

| AI | 值 | 說明 |
| --- | --- | --- |
| AI_18 | `1` | inverter 1 成功 bit |
| AI_19 | `0` | inverter 26 到 50 無成功 bit |
| AI_15 | `50` | Active Power Setpoint feedback |

### 12.3 AO_0 Power Factor Setpoint = 98%

DREAMS Master Simulator：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
.venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py \
  --host 127.0.0.1 --port 20000 \
  --master-address 100 --outstation-address 520 \
  ao 0 98
```

Outstation 發給 PV Logger：

```json
{
  "cmd_id": "8f05c9c9-4bc1-43aa-b6d0-25d01db11915",
  "ts": 1779955400,
  "type": "control",
  "target": "pf_percent",
  "value": 98,
  "unit": "%",
  "raw_ao_index": 0,
  "raw_value": 98
}
```

成功後 DNP3 feedback：

| AI | 值 | 說明 |
| --- | --- | --- |
| AI_18 | 依 `inverter_index` 設定 bit | 控制成功 bit |
| AI_19 | 依 `inverter_index` 設定 bit | 控制成功 bit |
| AI_14 | `98` | PF Setpoint feedback |

### 12.4 AO_3 Vpset = 105

DREAMS Master Simulator：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
.venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py \
  --host 127.0.0.1 --port 20000 \
  --master-address 100 --outstation-address 520 \
  ao 3 105
```

Outstation 發給 PV Logger：

```json
{
  "cmd_id": "5ce3330f-b1be-4314-b8c6-70cbe60cb3d8",
  "ts": 1779955450,
  "type": "control",
  "target": "vpset",
  "value": 105,
  "unit": "Int",
  "raw_ao_index": 3,
  "raw_value": 105
}
```

成功後 DNP3 feedback：

| AI | 值 | 說明 |
| --- | --- | --- |
| AI_17 | `105` | Vpset feedback |
| AI_18 / AI_19 | 依 `inverter_index` | 控制成功 bit |

### 12.5 AO_12 設定 AI_7 Active Power Deadband = 2.5%

DREAMS Master Simulator：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
.venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py \
  --host 127.0.0.1 --port 20000 \
  --master-address 100 --outstation-address 520 \
  ao 12 250
```

Outstation 發給 PV Logger：

```json
{
  "cmd_id": "e927eff9-5bff-44fb-ab99-0cb3253d8ee5",
  "ts": 1779955500,
  "type": "config_deadband",
  "target": "Deadband_AI_7",
  "value": 2.5,
  "unit": "%",
  "raw_ao_index": 12,
  "raw_value": 250
}
```

換算規則：

```text
value = raw_value * 0.01
250 * 0.01 = 2.5%
```

成功後 DNP3 feedback：

| AI | 值 | 說明 |
| --- | --- | --- |
| AI_27 | `2.5` | Active Power Dead Band Setting feedback |
| AI_18 / AI_19 | 依 `inverter_index` | 控制成功 bit |

### 12.6 Master UI 操作範例

用 Web UI 模擬 DREAMS Master：

1. 打開 Master Simulator UI：`http://127.0.0.1:8090/`
2. 在 `Registered DNP3 IDs` 找到 `logger_test02 / DNP3 520`。
3. 按 `Use`，讓 `Command DNP3 ID` 指向 `520`。
4. 在 `Single-ID Commands` 的 `Analog Output` 選擇 AO point。
5. 輸入 raw value。
6. 按 `Operate`。
7. PV Logger 應收到 `DREAMS/logger_test02/cmd`。
8. PV Logger 回覆 `DREAMS/logger_test02/cmd_ack`。
9. Master UI 的 `Received Events` 應看到 source=`cmd_ack` 的 AI_18 / AI_19 與 feedback AI。

## 13. AI 點表

PV Logger 在 `snapshot` / `event` 的 `data` 內使用 `AI_x` 作為 key。Payload 值請送工程值，Outstation 會依 scale 轉成 DNP raw。

| AI | 名稱 | Payload 工程值 | DNP raw scale | Event 可用 |
| --- | --- | --- | --- | --- |
| AI_0 | Line Current Phase A | A | x10 | 是 |
| AI_1 | Line Current Phase B | A | x10 | 是 |
| AI_2 | Line Current Phase C | A | x10 | 是 |
| AI_3 | Line Current Phase N | A | x10 | 是 |
| AI_4 | Line Voltage Phase AB | V | x100 | 是 |
| AI_5 | Line Voltage Phase BC | V | x100 | 是 |
| AI_6 | Line Voltage Phase AC | V | x100 | 是 |
| AI_7 | Active Power | W | x1 | 是 |
| AI_8 | Reactive Power | Var | x1 | 是 |
| AI_9 | Power Factor | % | x1 | 是 |
| AI_10 | Frequency | Hz | x10 | 是 |
| AI_11 | Accumulated Energy | Wh | x1 | 否，snapshot only |
| AI_12 | Irradiance | W/m2 | x1 | 否，snapshot only |
| AI_13 | Wind Speed | m/s | x1 | 否，snapshot only |
| AI_14 | Inverter PF Setpoint | % | x1 | 是 |
| AI_15 | Inverter Active Power Setpoint | % | x1 | 是 |
| AI_16 | Inverter Reactive Power Setpoint | % | x1 | 是 |
| AI_17 | Inverter Vpset | integer | x1 | 是 |
| AI_18 | Inverter 1-25 Control Success Bitmask | 25-bit integer | x1 | 由 `cmd_ack` 產生 |
| AI_19 | Inverter 26-50 Control Success Bitmask | 25-bit integer | x1 | 由 `cmd_ack` 產生 |
| AI_20 | Line Current Phase A Dead Band Setting | % | x100 | 是 |
| AI_21 | Line Current Phase B Dead Band Setting | % | x100 | 是 |
| AI_22 | Line Current Phase C Dead Band Setting | % | x100 | 是 |
| AI_23 | Line Current Phase N Dead Band Setting | % | x100 | 是 |
| AI_24 | Line Voltage Phase AB Dead Band Setting | % | x100 | 是 |
| AI_25 | Line Voltage Phase BC Dead Band Setting | % | x100 | 是 |
| AI_26 | Line Voltage Phase AC Dead Band Setting | % | x100 | 是 |
| AI_27 | Active Power Dead Band Setting | % | x100 | 是 |
| AI_28 | Reactive Power Dead Band Setting | % | x100 | 是 |
| AI_29 | Power Factor Dead Band Setting | % | x100 | 是 |
| AI_30 | Frequency Dead Band Setting | % | x100 | 是 |
| AI_31 | Spare | - | x1 | 預設停用 |
| AI_32 | Timestamp | Unix seconds | x1 | 由 `ts` 產生 |

AI_18 / AI_19 bitmask 規則：

| Inverter index | 使用點位 | Bit |
| --- | --- | --- |
| 1 到 25 | AI_18 | `1 << (inverter_index - 1)` |
| 26 到 50 | AI_19 | `1 << (inverter_index - 26)` |

範例：

- `inverter_index=1` 成功：`AI_18 = 1`
- `inverter_index=2` 成功：`AI_18 = 2`
- `inverter_index=26` 成功：`AI_19 = 1`

## 14. AO 命令對應表

PV Logger 收到 `cmd` 時主要看 `raw_ao_index`、`target`、`value`。

| AO | `type` | `target` | `value` 單位 | Feedback AI |
| --- | --- | --- | --- | --- |
| AO_0 | `control` | `pf_percent` | % | AI_14 |
| AO_1 | `control` | `active_power_percent` | % | AI_15 |
| AO_2 | `control` | `reactive_power_percent` | Var / %，目前保留 | AI_16 |
| AO_3 | `control` | `vpset` | integer | AI_17 |
| AO_4 | `control` | `autonomous_control` | - | 無 |
| AO_5 | `config_deadband` | `Deadband_AI_0` | % | AI_20 |
| AO_6 | `config_deadband` | `Deadband_AI_1` | % | AI_21 |
| AO_7 | `config_deadband` | `Deadband_AI_2` | % | AI_22 |
| AO_8 | `config_deadband` | `Deadband_AI_3` | % | AI_23 |
| AO_9 | `config_deadband` | `Deadband_AI_4` | % | AI_24 |
| AO_10 | `config_deadband` | `Deadband_AI_5` | % | AI_25 |
| AO_11 | `config_deadband` | `Deadband_AI_6` | % | AI_26 |
| AO_12 | `config_deadband` | `Deadband_AI_7` | % | AI_27 |
| AO_13 | `config_deadband` | `Deadband_AI_8` | % | AI_28 |
| AO_14 | `config_deadband` | `Deadband_AI_9` | % | AI_29 |
| AO_15 | `config_deadband` | `Deadband_AI_10` | % | AI_30 |

Deadband AO 換算：

- `raw_value` 單位是 `0.01%`
- `value = raw_value * 0.01`
- 例如 AO_12 `raw_value=250`，payload 會是 `value=2.5`、`unit=%`

## 15. DNP3 ID 綁定

DNP3 ID 不由 PV Logger 自行決定。流程如下：

1. Outstation UI 輸入電號與 token，向 DREAMS API / 模擬 API 取得一個或多個 `dnp3Address`。
2. Outstation UI 將 `logger_id` 綁定到指定 `dnp3Address`。
3. 綁定保存於 SQLite：`runtime.sqlite_path` 的 `logger_bindings` 表。
4. Outstation service 會每幾秒自動偵測綁定變更。
5. 綁定改變後，Outstation 會自動重建 DNP3 gateway，讓新的 DNP3 link address 生效。

PV Logger 需要做的事：

- 使用固定且唯一的 `logger_id`。
- 不要在 MQTT topic 內帶 `site_id`。
- 上線後先送 `status=online` 與 `snapshot`，讓 Outstation UI 可看到該 logger 並進行綁定。

Outstation 另外會在同一個 SQLite 檔維護 `command_log` 表，用於追蹤控制指令：

- DNP3 Master AO 或 Outstation UI 手動命令送出 MQTT `cmd` 時，記錄 `cmd_id`、`logger_id`、DNP3 ID、AO index、`type`、`target`、`value` 與完整 MQTT payload。
- PV Logger 回覆 `cmd_ack` 後，回填 ACK payload、`SUCCESS` / `FAILED` 狀態與錯誤訊息。
- 若 ACK 來自 DNP3 Master 指令且成功，Outstation 會同時記錄送回 DNP3 的 feedback AI，例如 `AI_18`、`AI_19`、`AI_15` 或 deadband feedback AI。

## 16. 建議開機與運行流程

PV Logger 啟動：

1. 連線 MQTT broker。
2. 訂閱 `DREAMS/{logger_id}/cmd`。
3. 發布 `DREAMS/{logger_id}/status`，內容為 `online`。
4. 發布 `DREAMS/{logger_id}/snapshot`，`reason=startup`。
5. 進入週期回報：
   - 每 15 分鐘發布 `snapshot`，`reason=periodic`
   - 量測點超過 deadband 時發布 `event`
6. 收到 `cmd` 後執行命令並發布 `cmd_ack`。

PV Logger 關機或失聯前若可預期：

```json
{
  "ts": 1779955500,
  "status": "offline",
  "message": "logger shutting down"
}
```

## 17. MQTT 測試指令範例

訂閱指定 logger：

```bash
mosquitto_sub -h 34.80.23.92 -p 1883 -u dev -P '<password>' -t 'DREAMS/logger_test02/#' -v -q 1
```

發布 status：

```bash
mosquitto_pub -h 34.80.23.92 -p 1883 -u dev -P '<password>' \
  -t 'DREAMS/logger_test02/status' -q 1 -r false \
  -m '{"ts":1779955200,"status":"online"}'
```

發布 snapshot：

```bash
mosquitto_pub -h 34.80.23.92 -p 1883 -u dev -P '<password>' \
  -t 'DREAMS/logger_test02/snapshot' -q 1 -r false \
  -m '{"ts":1779955200,"reason":"periodic","data":{"AI_4":23000,"AI_7":52000,"AI_10":60}}'
```

發布 event：

```bash
mosquitto_pub -h 34.80.23.92 -p 1883 -u dev -P '<password>' \
  -t 'DREAMS/logger_test02/event' -q 1 -r false \
  -m '{"ts":1779955261,"reason":"deadband","data":{"AI_7":53120}}'
```

發布 cmd_ack：

```bash
mosquitto_pub -h 34.80.23.92 -p 1883 -u dev -P '<password>' \
  -t 'DREAMS/logger_test02/cmd_ack' -q 1 -r false \
  -m '{"ts":1779955310,"cmd_id":"cmd-1","status":"SUCCESS","inverter_index":1,"raw_ao_index":1,"target":"active_power_percent","value":50}'
```

## 18. 驗收檢查清單

PV Logger 實作完成後，至少確認：

- MQTT 連線可成功登入 broker。
- 使用 topic `DREAMS/{logger_id}/...`，沒有 `site_id`。
- `status=online` 可在 Outstation UI 的 Loggers 顯示。
- `snapshot` 可在 Outstation UI 即時點值畫面顯示。
- `event` 只送變動點，且可在 Master Simulator `Received Events` 看到 source=`event`。
- 收到 `cmd` 後會用相同 `cmd_id` 回覆 `cmd_ack`。
- `cmd_ack SUCCESS` 後 Master Simulator 可看到 source=`cmd_ack`，並包含 AI_18 / AI_19 與 feedback AI。
- QoS 1 重複 `cmd_id` 不會造成重複控制。
- MQTT publish 不使用 retained message，避免舊資料或舊命令被新連線誤用。

## 19. 目前版本限制

- `cmd_ack` 目前以一個 `cmd_id` 對應一筆回覆為主。
- `FAILED` 不會產生 DNP3 成功 bit event。
- 多 inverter 部分成功/部分失敗需要後續擴充 schema。
- `AI_31` 預設停用。
- DNP3 event class 受 `pydnp3/opendnp3` 限制，目前用 flag bit 7 區分 periodic/deadband。
