import csv
from datetime import datetime, timedelta, timezone
import os
import re
import time
from bs4 import BeautifulSoup
import requests

# ─── Webhook設定 ──────────────────────────────────────
# ① 速報通知用
DISCORD_WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1564260115627118712/foCYPCGT9U4WKFiIbpbzOUk61Uj1L_kCgjb7sjK9Sah3ffqWFoGXn6X_JXw6LN5kAZvo",
)

# ② 運行終了ログ専用
DISCORD_LOG_WEBHOOK_URL = os.environ.get(
    "DISCORD_LOG_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1564501173560680510/W3TyjQit7bR4rjcj3x1dnRdQB4xF2M9wRbI6dgVfL2uCQl8NrbCj3nr9UCJF18BHTKCS",
)

# ─── 夜間休止・特定日夜間運用設定 ─────────────────────
ENABLE_NIGHT_SLEEP = True  # 夜間休止機能を使うか (True / False)
NIGHT_START_HOUR = 1  # 休止開始（深夜1時）
NIGHT_END_HOUR = 5  # 休止終了（早朝5時）

# ★夜間も監視したい日の日付（YYYY-MM-DD形式）
OVERNIGHT_ACTIVE_DATES = [
    # "2026-10-31",
]

# ─── 路線設定 ─────────────────────────────────────────
TARGET_LINES = {
    "01": "難波線・奈良線",
    # "02": "生駒線",
    "03": "京都線",
    "04": "橿原線",
    "05": "天理線",
    # "06": "田原本線",
    "07": "大阪線（大阪上本町〜西青山）",
    "08": "大阪線（東青山〜伊勢中川）",
    # "09": "信貴線",
    "10": "名古屋線",
    # "11": "湯の山線",
    # "12": "鈴鹿線",
    "13": "山田線",
    "14": "鳥羽線",
    "15": "志摩線",
    # "16": "南大阪線",
    # "17": "吉野線",
    # "18": "道明寺線",
    # "19": "長野線",
    # "20": "御所線",
}

BASE_URL = "https://tid.kintetsu.co.jp/LocationWeb/trainlocationinfoweb{}.html"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

LOG_DIR = "logs"
notified_trains = set()
active_trains = {}


def is_night_sleep_time(now_dt):
  """夜間休止判定"""
  if not ENABLE_NIGHT_SLEEP:
    return False

  today_str = now_dt.strftime("%Y-%m-%d")
  if today_str in OVERNIGHT_ACTIVE_DATES:
    return False

  return NIGHT_START_HOUR <= now_dt.hour < NIGHT_END_HOUR


def get_line_stations(soup):
  """駅名リスト取得"""
  stations = []
  for tag in soup.find_all(class_="station-name"):
    name = tag.get_text(strip=True).replace("\u3000", "").replace(" ", "")
    if name and name not in stations:
      stations.append(name)
  return {i + 1: name for i, name in enumerate(stations)}


def resolve_train_location(box, stations_dict, direction="下り"):
  """進行方向に応じた駅間矢印表記"""
  parent_cell = box.find_parent(
      lambda t: t.get("class")
      and any(c.startswith("cell-") for c in t.get("class"))
  )
  if not parent_cell:
    return "位置不明", None

  classes = parent_cell.get("class", [])
  cell_num = None
  for c in classes:
    m = re.match(r"^cell-(\d+)$", c)
    if m:
      cell_num = int(m.group(1))
      break

  if cell_num is None:
    return "位置不明", None

  if cell_num % 2 == 1:
    st_idx = (cell_num + 1) // 2
    location_name = stations_dict.get(st_idx, f"駅番号{st_idx}")
  else:
    st_idx1 = cell_num // 2
    st_idx2 = st_idx1 + 1
    st1 = stations_dict.get(st_idx1, f"駅番号{st_idx1}")
    st2 = stations_dict.get(st_idx2, f"駅番号{st_idx2}")

    # 上り・下りで矢印の向きを切り替え
    if direction == "上り":
      location_name = f"{st2} → {st1} 間"
    else:
      location_name = f"{st1} → {st2} 間"

  return location_name, cell_num


def parse_delay_text(box):
  """遅延判定"""
  delay_tag = box.find("span", class_="delay-time")
  if not delay_tag:
    return "🟢 平常運転"

  raw_text = delay_tag.get_text(strip=True)
  if not raw_text.isdigit():
    return "🟢 平常運転"

  delay_sec = int(raw_text)
  if delay_sec <= 59:
    return "🟢 平常運転"
  elif delay_sec <= 5989:
    delay_min = delay_sec // 60
    return f"⚠️ 約{delay_min}分遅れ"
  else:
    return "🚨 大幅遅れ"


def save_train_log(
    line_name, direction, dest, location_name, delay_text, now_str
):
  """ローカルCSV保存"""
  os.makedirs(LOG_DIR, exist_ok=True)
  safe_dest = re.sub(r'[\\/:*?"<>|]', "", dest) if dest else "行先なし"
  jst = timezone(timedelta(hours=9))
  date_str = datetime.now(jst).strftime("%Y%m%d")
  filename = os.path.join(
      LOG_DIR, f"{date_str}_{line_name}_{direction}_{safe_dest}.csv"
  )
  file_exists = os.path.exists(filename)

  with open(filename, mode="a", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    if not file_exists:
      writer.writerow(
          ["検知日時", "路線", "進行方向", "行先", "現在位置", "運行状況"]
      )
    writer.writerow(
        [now_str, line_name, direction, dest, location_name, delay_text]
    )


def send_discord_embed(
    line_name, direction, dest, location_name, delay_text, line_code, now_str
):
  """速報Embed送信"""
  if not DISCORD_WEBHOOK_URL:
    return

  web_url = BASE_URL.format(line_code)
  payload = {
      "content": "🚨 **貸切列車を検知しました！** @everyone",
      "embeds": [{
          "title": f"近鉄 貸切列車 運行情報 ({line_name})",
          "url": web_url,
          "color": 16729871,
          "fields": [
              {"name": "路線", "value": line_name, "inline": True},
              {"name": "進行方向", "value": direction, "inline": True},
              {
                  "name": "行先表示",
                  "value": dest if dest else "なし",
                  "inline": True,
              },
              {"name": "現在位置", "value": location_name, "inline": True},
              {"name": "運行状況", "value": delay_text, "inline": True},
              {"name": "検知時刻", "value": now_str, "inline": False},
          ],
          "footer": {"text": "タップすると近鉄公式の走行位置画面を開きます"},
      }],
  }
  try:
    requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
  except Exception as e:
    print(f"速報通知送信エラー: {e}")


def send_finish_log_to_discord(train_info):
  """運行終了ログ送信"""
  target_webhook = DISCORD_LOG_WEBHOOK_URL or DISCORD_WEBHOOK_URL
  if not target_webhook:
    return

  line_name = train_info["line_name"]
  direction = train_info["direction"]
  dest = train_info["dest"]
  history = train_info["history"]

  if not history:
    return

  start_time = history[0]["time"]
  end_time = history[-1]["time"]
  start_loc = history[0]["location"]
  end_loc = history[-1]["location"]

  lines = [f"{h['time']} | {h['location']} ({h['delay']})" for h in history]
  log_body = "\n".join(lines)

  header = (
      f"🏁 **【運行終了】貸切列車の追跡ログ ({line_name})**\n"
      f"・**方向**: {direction}\n"
      f"・**行先**: {dest if dest else 'なし'}\n"
      f"・**走行区間**: {start_loc} ➔ {end_loc}\n"
      f"・**追跡期間**: {start_time} 〜 {end_time}\n"
      f"```text\n"
  )
  footer = "\n```"

  max_body_len = 2000 - len(header) - len(footer) - 20
  if len(log_body) > max_body_len:
    log_body = log_body[:max_body_len] + "\n... (以降省略)"

  message = header + log_body + footer

  try:
    res = requests.post(
        target_webhook, json={"content": message}, timeout=10
    )
    print(
        f"[{end_time}] ログ専用チャンネルへ運行終了ログを送信完了 ({line_name} /"
        f" ステータス: {res.status_code})"
    )
  except Exception as e:
    print(f"運行終了ログ送信エラー: {e}")


def scan_once():
  jst = timezone(timedelta(hours=9))
  now_str = datetime.now(jst).strftime("%Y/%m/%d %H:%M:%S")
  current_seen_train_ids = set()

  for line_code, line_name in TARGET_LINES.items():
    url = BASE_URL.format(line_code)
    try:
      res = requests.get(url, headers=HEADERS, timeout=10)

      if res.status_code == 429:
        print(
            f"⚠️ [{line_name}] レート制限 (429) を検知。BAN防止のため5分間待機します..."
        )
        time.sleep(300)
        return False
      elif res.status_code >= 500:
        print(
            f"⚠️ [{line_name}] サーバーエラー ({res.status_code})"
            " を検知。2分間待機します..."
        )
        time.sleep(120)
        continue
      elif res.status_code != 200:
        time.sleep(1)
        continue

      res.encoding = res.apparent_encoding

    except requests.exceptions.Timeout:
      print(
          f"⏱️ [{line_name}] タイムアウトが発生しました。15秒待機して継続します。"
      )
      time.sleep(15)
      continue
    except requests.exceptions.RequestException as e:
      print(f"通信エラー発生 ({line_name}): {e}。10秒待機します。")
      time.sleep(10)
      continue
    except Exception as e:
      print(f"予期せぬエラー ({line_name}): {e}")
      time.sleep(1)
      continue

    soup = BeautifulSoup(res.text, "html.parser")
    stations = get_line_stations(soup)
    train_boxes = soup.find_all(
        "div", class_=["train-info-up", "train-info-down"]
    )

    for box in train_boxes:
      # 方向判定修正箇所
      direction = "上り" if "train-info-down" in box.get("class", []) else "下り"
      dest_tag = box.find("div", class_="train-destination-text")
      destination = dest_tag.get_text(strip=True) if dest_tag else ""
      img_tag = box.find("img")
      img_src = img_tag.get("src", "") if img_tag else ""

      delay_text = parse_delay_text(box)

      is_target = (
          "09.png" in img_src
          or "貸切" in destination
          or "貸切" in box.get_text()
      )

      if is_target:
        location_name, cell_num = resolve_train_location(
            box, stations, direction
        )
        train_id = f"{line_code}_{direction}_{destination}"
        current_seen_train_ids.add(train_id)

        if train_id not in active_trains:
          active_trains[train_id] = {
              "line_name": line_name,
              "direction": direction,
              "dest": destination,
              "history": [],
              "missed_count": 0,
          }

        train_data = active_trains[train_id]
        train_data["missed_count"] = 0

        last_recorded_loc = (
            train_data["history"][-1]["location"]
            if train_data["history"]
            else None
        )
        if location_name != last_recorded_loc:
          train_data["history"].append({
              "time": now_str,
              "location": location_name,
              "delay": delay_text,
          })
          save_train_log(
              line_name,
              direction,
              destination,
              location_name,
              delay_text,
              now_str,
          )

        train_key = f"{train_id}_{cell_num}"
        if train_key not in notified_trains:
          notified_trains.add(train_key)
          print(
              f"【検知】{line_name} / {direction} / {location_name} /"
              f" {delay_text}"
          )
          send_discord_embed(
              line_name,
              direction,
              destination,
              location_name,
              delay_text,
              line_code,
              now_str,
          )

    time.sleep(1)

  # 終了判定
  finished_train_ids = []
  for train_id, train_data in active_trains.items():
    if train_id not in current_seen_train_ids:
      train_data["missed_count"] += 1
      if train_data["missed_count"] >= 2:
        send_finish_log_to_discord(train_data)
        finished_train_ids.append(train_id)

  for t_id in finished_train_ids:
    del active_trains[t_id]

  return len(active_trains) > 0


if __name__ == "__main__":
  count = 1
  jst = timezone(timedelta(hours=9))
  print("=== 近鉄 貸切監視システム（安全制御・夜間管理稼働中） ===")

  while True:
    now_dt = datetime.now(jst)

    if is_night_sleep_time(now_dt):
      print(
          f"[{now_dt.strftime('%H:%M:%S')}] 🌙 夜間休止時間帯（{NIGHT_START_HOUR}:00〜{NIGHT_END_HOUR}:00）です。15分待機します。"
      )
      time.sleep(900)
      continue

    print(f"\n[{now_dt.strftime('%H:%M:%S')}] 監視スキャン実施 (#{count})")
    has_kashikiri = scan_once()
    count += 1

    if has_kashikiri:
      wait_sec = 60
      print(
          f"[{datetime.now(jst).strftime('%H:%M:%S')}] 🚨"
          " 貸切検知中！1分後に再追跡します。"
      )
    else:
      wait_sec = 180
      print(
          f"[{datetime.now(jst).strftime('%H:%M:%S')}] 貸切なし。3分間待機します。"
      )

    time.sleep(wait_sec)
