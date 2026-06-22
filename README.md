# DREAMS Outstation

這個資料夾現在是一個獨立 Python 專案，目標是在雲端固定 IP 主機上提供 DREAMS DNP3 Outstation，並透過 MQTT broker `34.80.23.92:1883` 和 PV Gateway/DataLogger 溝通。

## 架構

```text
DREAMS DNP3 Master
        |
        | DNP3 TCP 20000
        v
DREAMS Outstation (this project)
        |
        | MQTT DREAMS/{logger_id}/...
        v
MQTT Broker 34.80.23.92:1883
        |
        v
PV Gateway / DataLogger
```

PV Logger 開發者可先看 MQTT 技術規格書：[docs/PV_LOGGER_MQTT_TECH_SPEC.md](docs/PV_LOGGER_MQTT_TECH_SPEC.md)。

## 已實作

- MQTT 訂閱 `event`、`snapshot`、`status`、`cmd_ack`
- MQTT 發布 DREAMS 控制轉發 `cmd`
- MQTT topic 使用 `DREAMS/{logger_id}/{suffix}`；DNP3 ID 綁定主鍵以 `logger_id` 維護
- 可依規範用 REST API 向 DREAMS 模擬伺服器取得案場 DNP3.0 ID
- 台電附錄一太陽光電 AI/AO 點表
- `AI_11` 依台電附錄一使用 Object 30/32 Variation 6
- `AI_31` 預設保留不送，可在 config 開啟
- 每案場 SQLite FIFO 暫存 1024 筆事件
- Class 1 定時完整資料與 Class 2 Dead Band 事件的 flag bit 規則
- DNP3 adapter 使用 `pydnp3`，不可用時會切到 buffer-only 模式
- systemd service 範本

## 本機測試

PowerShell:

```powershell
$env:PYTHONPATH="src"
python -m unittest discover -s tests
```

## 啟動

第一次使用先建立本機設定檔，`config/config.yaml` 內會放 MQTT 密碼與 DREAMS token，預設不提交到 Git：

```bash
cp config/config.example.yaml config/config.yaml
```

```powershell
$env:PYTHONPATH="src"
python -m dreams_outstation --config config/config.yaml
```

目前 Windows 本機未安裝 `pydnp3`，啟動後會使用 buffer-only 模式。正式雲端 Linux 主機需安裝 DNP3 optional dependency。

## 操作介面

Web UI 會以 sidecar 方式啟動，不會取代 DNP3 Outstation service。

```bash
PYTHONPATH=src python3 -m dreams_outstation.web_ui --config config/config.yaml --host 0.0.0.0 --port 8088
```

預設登入帳密為 `admin` / `dreams`。可用環境變數覆蓋：

```bash
DREAMS_UI_USERNAME=admin DREAMS_UI_PASSWORD='change-me' \
  PYTHONPATH=src python3 -m dreams_outstation.web_ui --config config/config.yaml
```

本機可打開 `http://127.0.0.1:8088`；同網段設備可用本機區網 IP，例如 `http://172.20.10.3:8088`。打開後可查看：

- DNP3 TCP 20000 listen 狀態
- MQTT broker TCP 連線狀態
- service PID 與 SQLite buffer 數量
- AI/AO 點表
- UI 透過 MQTT 訂閱到的即時 AI 點值
- DREAMS API endpoint、電號/token 設定、DNP3 ID 查詢結果
- MQTT `logger_id` / DNP3 ID 綁定維護與解除綁定，保存於 SQLite
- 最新 log
- 手動送出 AO 測試 command 到 MQTT `cmd` topic

### DREAMS DNP3 ID API

規範要求 Outstation 雲端資料系統用 API 向 DREAMS 模擬伺服器取得案場 DNP3.0 ID。本專案支援：

```yaml
dreams_api:
  enabled: false
  base_url: "http://127.0.0.1:8090"
  plant_meter_no: "test-meter"
  site_token: "test-token"
  apply_to_sites: true
```

API 形狀為 `GET /api/plants/plantMeterNo/<電號>?token=<siteToken>`，回傳資料中以 `dnp3Address` 作為綁定用的 DNP3 ID。`enabled: true` 時 Outstation 啟動會查詢並把 `dnp3Address` 套用到對應 logger；Web UI 的 DREAMS API 區塊可手動查詢與顯示 DNP3 ID。

綁定會存在同一個 SQLite 檔 `runtime.sqlite_path` 的 `logger_bindings` 表。UI 會用 MQTT live 資料看到的實際 `logger_id` 作為綁定主體，例如 `logger_test01 -> DNP3 ID 1`；舊版 `site_bindings` 資料會在啟動時自動搬到 `logger_bindings`。保存後 UI 會立即顯示新綁定；Outstation 服務會每幾秒檢查資料庫綁定是否改變，若 DNP3 ID 或 logger 綁定有變更，會自動重建 DNP3 gateway 讓新的 link address 生效，MQTT 連線不需要人工重啟。

Outstation UI 也會顯示 `Command Log`。DNP3 Master 下 AO 或 Outstation UI 手動送 MQTT `cmd` 時，系統會將 `cmd_id`、logger、DNP3 ID、AO、`type`、`target`、MQTT payload、`cmd_ack` payload、狀態與 DNP3 feedback AI 寫入 SQLite 的 `command_log` 表，方便追蹤 `DNP3 AO -> MQTT cmd -> MQTT cmd_ack -> DNP3 feedback` 的完整流程。

## DNP3 Master Simulator

本機可用 `tools/dnp3_master_simulator.py` 模擬台電 DREAMS Master 端，連到本專案的 DNP3 Outstation `TCP 20000`。

若要用網頁操作，啟動 Master UI：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
PYTHONPATH=src \
.venv-dnp3-py310/bin/python tools/dnp3_master_ui.py --host 0.0.0.0 --port 8090
```

本機可打開 `http://127.0.0.1:8090`；同網段設備可用本機區網 IP，例如 `http://172.20.10.3:8090`。打開後，左側操作依照實際流程分成 `DNP3 Endpoint`、`Registered DNP3 IDs`、`Multi-ID Monitor`、`Master Poll`、`Single-ID Analog Output`。`Registered DNP3 IDs` 會即時讀取 SQLite 綁定後的 effective config，顯示已註冊 logger、DNP3 ID、Monitor 勾選與 `Use` 按鈕；Outstation UI 新增或修改 logger/DNP3 ID 綁定後，Master UI 會自動刷新清單。Monitor 勾選清單同時也是 `Master Poll` 目標，`Read AI` 與 `Scan Events` 會對每個勾選的 DNP3 ID 逐一輪詢並合併顯示結果。`Use` 只會把該 DNP3 ID 帶入 `Analog Output DNP3 ID`，不影響 Monitor / Poll 勾選；目前 AO 目標會在 `DNP3 Endpoint` 和清單列上標示。只有 `Analog Output` 會送到單一 AO 目標。AO UI 固定使用 DREAMS 控制流程的 Object 41 Variation 2 / Function 5 Direct Operate；Master Address 預設 `100`、Wait 預設 `8` 秒，作為模擬器內部 DNP3 連線參數，不顯示在一般操作畫面。

Master UI 同時提供 DREAMS DNP3 ID 模擬 API：

```text
GET http://127.0.0.1:8090/api/plants/plantMeterNo/test-meter?token=test-token
```

可用 `--plant-meter-no` 與 `--site-token` 覆蓋測試電號與 token。
模擬 API 會讓設定電號回傳目前 effective config 內的 DNP3 ID；輸入其他電號時，會依電號產生穩定的測試 DNP3 ID 清單，方便在 Outstation UI 測試多個電號與多筆 DNP3 ID 分配。

若要模擬 DREAMS Master 常駐接收 Outstation 主動回報，先在 `Registered DNP3 IDs` 清單勾選要監看的 DNP3 ID，再按 `Start Monitor`。Monitor 會對每個勾選的 DNP3 ID 建立長連線、送出 `ENABLE_UNSOLICITED`，之後等待 Outstation 主動送 Class 1 / event 類資料；它不會每秒送 DNP3 polling。Web UI 只在 Monitor 執行中每 3 秒讀取一次本機後端狀態，以更新 `Received Events`、Console 與按鈕狀態。`Received Events` 的 `Source` 會用 `snapshot`、`event`、`cmd_ack` 表示來源：`event` 對應 MQTT `event` topic 的 deadband/變動資料，`cmd_ack` 對應 MQTT `cmd_ack` topic 的指令回饋；DNP3 封包不帶 MQTT topic 名稱，因此 Master UI 會用同一批非週期事件是否包含 AI_18/AI_19 來推斷 `cmd_ack`。Monitor 執行中仍可對勾選的多個 DNP3 ID 執行 `Read AI`、`Scan Events`，並可用 `Analog Output DNP3 ID` 對單一 DNP3 ID 執行 AO command。

工研院 DREAMS 測試逐項驗證流程整理在 `docs/ITRI_DREAMS_TEST_VALIDATION.md`。其中 6-1 / 6-2 需要 PV Logger 或 MQTT broker 在資料蒐集器斷電/復歸時送出 `status=offline` / `status=online` 與完整 `snapshot`；Outstation 收到 offline 後會停用該 logger 對應的 DNP3 ID，讓 Master 對該 ID 的 poll / keep alive 無回應並判定離線。TCP server 是否可連只代表 Outstation service 是否活著，不代表每一台機器在線；收到 online/snapshot/event 後會恢復該 logger 的 DNP3 ID 回應。

先啟動 Outstation：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
PYTHONPATH=src \
.venv-dnp3-py310/bin/python -m dreams_outstation --config config/config.yaml
```

讀取 AI_0~AI_32：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
.venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py \
  --host 127.0.0.1 --port 20000 \
  --master-address 100 --outstation-address 1 \
  range 0 32
```

模擬台電下 AO，例如 AO_1 active power setpoint = 50：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
.venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py \
  --host 127.0.0.1 --port 20000 \
  --master-address 100 --outstation-address 1 \
  ao 1 50
```

AO 預設使用 DREAMS 規格的 Object 41 Variation 2，也就是 16-bit Analog Output Block。可用 `--mode direct` 模擬 Function 5 Direct Operate，或用 `--mode sbo` 模擬 Function 3/4 Select + Operate。DREAMS profile 有列 Function 6 Direct Operate No ACK，但目前 `pydnp3 0.1.0` 沒有暴露可帶 AO payload 的 No ACK command API，因此本模擬器會明確回報不支援。

常駐監看 unsolicited/event：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
.venv-dnp3-py310/bin/python tools/dnp3_master_simulator.py \
  --host 127.0.0.1 --port 20000 \
  --master-address 100 --outstation-address 1 \
  monitor --enable-unsolicited
```

地址設定需保持 Master 與 Outstation 不同。`config/config.yaml` 目前本機模擬設定為 Master address `100`、Outstation address `1`。

## Linux 部署重點

```bash
python3 -m venv /opt/dreams-outstation/.venv
/opt/dreams-outstation/.venv/bin/pip install -r requirements.txt
/opt/dreams-outstation/.venv/bin/pip install -r requirements-dnp3.txt
```

`pydnp3` 是 source-only package，需要 Linux/MacOS、C++14 compiler、CMake。安裝後：

```bash
/opt/dreams-outstation/.venv/bin/python -m dreams_outstation --config /etc/dreams-outstation/config.yaml
```

雲端防火牆需開：

- Inbound TCP `20000` 給 DREAMS/ITRI 模擬平台
- Outbound TCP `1883` 到 MQTT broker `34.80.23.92`

## 尚待 DREAMS/測試單位提供

- siteToken 與電號查詢 API 的正式資訊
- 正式 DNP3 address，現在 config 暫用測試 ID `1`
- DREAMS Master IP 白名單
- ITRI lab 對 `pydnp3` event class 配置的驗證結果

注意：`pydnp3/opendnp3` 對每個 AI 點只能設定單一 event class；目前預設使用 Class 2，並用 Object 32 Var 3 flag bit 7 區分定時/Dead Band。MQTT `event` topic 對應 deadband/變動資料；MQTT `cmd_ack` topic 是 AO 指令回饋，DNP3 端同樣會以非週期 event 類資料送出 AI_18/AI_19 與對應 feedback AI。若 ITRI 測試要求同一 AI 點須同時用 Class 1 定時與 Class 2 Dead Band，需要再調整 DNP3 stack 或改用可客製 event class 的實作。
