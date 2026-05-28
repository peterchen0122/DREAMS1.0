# ITRI DREAMS 測試驗證清單

來源文件：`工研院Dreams雲端測試規範說明_20250918.pdf`

## 測試前必備

- 固定 IP 對外開放 DNP3 TCP `20000`，防火牆與 NAT 需允許工研院 DREAMS 模擬平台連入。
- Outstation 使用 `pydnp3` backend 啟動，不能使用 `runtime.dnp3_backend: null`。
- Outstation UI 至少綁定 2 個 logger：
  - DNP3 ID `1`：現場測試設備，會接受控制。
  - DNP3 ID `2`：自備真實案場，只提供資料，不做控制。
- PV Logger 必須實作 MQTT：
  - 上線：發布 `DREAMS/{logger_id}/status`，`status=online`。
  - 啟動或 MQTT 重連：發布完整 `snapshot`，`reason=startup`。
  - 整點、15、30、45 分：發布完整 `snapshot`，`reason=periodic`。
  - Dead Band 觸發：3 秒內發布 `event`，`reason=deadband`。
  - 收到 `cmd` 後：執行設備控制或 deadband 設定，發布 `cmd_ack`。
  - 斷電/離線：發布 `status=offline`，或由 MQTT broker Last Will 代送 offline。
- 現場設備、雲端主機與 PV Logger 建議都開 NTP 校時。

## Master UI 驗證入口

啟動 Master UI：

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
PYTHONPATH=src \
.venv-dnp3-py310/bin/python tools/dnp3_master_ui.py --host 0.0.0.0 --port 8090
```

操作原則：

- `Registered DNP3 IDs` 的勾選清單同時是 `Multi-ID Monitor` 與 `Master Poll` 目標。
- `Master Poll` 的 `Read AI` / `Scan Events` 會對所有勾選 DNP3 ID 逐一讀取。
- `Use` 只設定 `Single-ID Analog Output` 的 AO 目標；AO 一次只送單一 DNP3 ID。

## 項目對照表

| 項目 | 驗證方式 | 通過證據 | 注意事項 |
| --- | --- | --- | --- |
| 1-1 連線 | Master UI 對 Outstation 執行 `Start Monitor` 或 `Read AI`。 | Monitor Console 出現 link open，或 `Read AI` 成功回資料。 | 固定 IP、TCP `20000`、Master Address `100` 需正確。 |
| 1-2 兩案場 | 在 `Registered DNP3 IDs` 看到 ID `1` 與 ID `2`，兩者皆勾選後啟動 Monitor。 | `Monitoring: 1, 2`，兩案場皆可 Poll / Monitor。 | ID `1` 是現場設備；ID `2` 是自備真實案場。 |
| 2-1 Poll 數值 | `Master Poll` 按 `Read AI`。 | `Polled AI` 顯示兩個 DNP3 ID 的 AI_0..AI_32；AI_0..AI_10 與現場電表一致。 | PV Logger payload 使用工程值，Outstation 會轉 DNP raw。 |
| 2-2 Dead Band 預設值 | `Read AI` 檢查 AI_20..AI_30。 | AI_20..AI_26、AI_30 為 `0.5%`；AI_27..AI_29 為 `1.0%`。 | DNP raw 會是 `50` 或 `100`，UI value 會換算回百分比。 |
| 2-3 Dead Band event | 先 `Start Monitor`，再降低變流器輸出電流。 | 3 秒內 `Received Events` 出現 source=`event`，帶 DNP3 timestamp。 | PV Logger 必須每秒檢查 deadband，並在觸發後送 `event` topic。 |
| 2-4 15 分完整資料 | Monitor 持續開啟，等待 00、15、30、45 分。 | 收到 source=`snapshot`、DNP3 Type=`periodic` 的完整資料，flags 含 `0x80`。 | PV Logger 必須準時送 `reason=periodic` 的完整 snapshot。 |
| 3-1 取消自主調控 | AO 目標選 ID `1`，送 AO_4 raw value `0`。 | 現場確認變流器接受外部調控；`Received Events` 出現 source=`cmd_ack` 與 AI_18/AI_19 成功 bit。 | AO_4：`0` 關閉自主調控，`1` 開啟自主調控。 |
| 3-2 PF 90 | 送 AO_0 raw value `90`。 | 變流器/電表 PF 逐漸接近 90%，誤差 3% 內；AI_14 feedback=`90`；AI_18/19 成功。 | 電表 PF deadband 應回傳 AI_9。 |
| 3-3 PF 100 | 送 AO_0 raw value `100`。 | 變流器/電表 PF 逐漸接近 100%，誤差 3% 內；AI_14 feedback=`100`；AI_18/19 成功。 | 同上。 |
| 3-4 P 80 | 送 AO_1 raw value `80`。 | 電表 P 值接近額定功率 80%，誤差 3% 內；AI_15 feedback=`80`；AI_18/19 成功。 | PV Logger 需用變流器額定功率換算實功目標。 |
| 3-5 P 100 | 送 AO_1 raw value `100`。 | 電表 P 回復最大輸出功率，誤差 3% 內；AI_15 feedback=`100`；AI_18/19 成功。 | 同上。 |
| 3-6 恢復自主調控 | 送 AO_4 raw value `1`。 | 現場確認在自主調控狀態；AI_18/19 成功。 | AO_4 沒有 feedback AI，只看成功 bit 與現場狀態。 |
| 3-7 Vpset 105 | 送 AO_3 raw value `105`。 | 現場 Vpset 變更為 105；AI_17 feedback=`105`；AI_18/19 成功。 | 規範值通常為 105..109。 |
| 3-8 P Dead Band 2.5% | 送 AO_12 raw value `250`。 | AI_27 feedback 顯示 `2.5%`，DNP raw=`250`；AI_18/19 成功。 | AO_12 單位是 `0.01%`，所以 2.5% 要輸入 raw `250`。 |
| 4-1 DREAMS 離線 31 分後恢復 | 保持 Outstation 與 PV Logger 運行，只中斷 Master/DREAMS 連線 31 分鐘後恢復。 | 恢復連線後兩案場在線，至少收到 2 筆離線期間的 periodic snapshot/event。 | 不要停 Outstation service；DNP3 event buffer 才能累積離線期間資料。 |
| 5-1 雲端系統離線 | 停止 Outstation service 或阻斷固定 IP DNP3 TCP。 | Master/DREAMS 看到所有案場離線或連線失敗。 | 這是雲端系統整體離線，不是單一 logger 離線。 |
| 5-2 雲端系統重啟 | 重啟 Outstation service，PV Logger 在 MQTT 重連後送 `status=online` 與完整 `snapshot`。 | Master/DREAMS 可 Poll 到兩案場最近一筆完整資料。 | 若 PV Logger 不送 startup snapshot，Outstation 重啟後沒有最新值可顯示。 |
| 6-1 現場資料蒐集器斷電 | 關閉 ID `1` 的 PV Logger/資料蒐集器，讓 broker LWT 或系統送 `status=offline`。 | Master/DREAMS 看到 ID `1` 離線；ID `2` 不受影響。 | Outstation 收到 `status=offline` 後會停用該 DNP3 outstation。 |
| 6-2 現場資料蒐集器復歸 | ID `1` 復電後送 `status=online` 與完整 `snapshot`。 | Master/DREAMS 收到 ID `1` 最近一筆定時資料，Poll / Monitor 恢復。 | `snapshot` 必須是完整資料。 |
| 7-1 固定 IP Nessus | 對受測固定 IP 做 Basic Network Scan。 | 報告無 Medium 以上弱點。 | 與程式功能無關，但會卡驗證。 |
| 7-2 Web Portal Nessus | 對受測 Web Portal 做 Web Application Test。 | 報告無 Medium 以上弱點。 | Outstation UI 若對外開放，需改強密碼、限制來源 IP、避免預設帳密。 |

## 現場必收證據

- Master UI `DNP3 Messages` 與 `Monitor Console` 截圖。
- `Received Events` 截圖，需包含 `Source`、`DNP3 Type`、`DNP3 ID`、`Point`、`Value`、`Flags`、`Received`。
- Outstation log：`logs/dreams-outstation.log`。
- MQTT broker 訊息紀錄：`snapshot`、`event`、`status`、`cmd`、`cmd_ack`。
- 現場電表與變流器畫面截圖，尤其是 PF/P/Vpset 控制測項。
- Nessus Basic Network Scan 與 Web Application Test 報告。
