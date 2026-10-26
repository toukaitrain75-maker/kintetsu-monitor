import os
import re
import time
import requests
from bs4 import BeautifulSoup

# GitHub SecretsからURLを読み込み
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# 近鉄 全20路線 完全対応
TARGET_LINES = {
    "01": "難波線・奈良線",
    "02": "生駒線",
    "03": "京都線",
    "04": "橿原線",
    "05": "天理線",
    "06": "田原本線",
    "07": "大阪線（大阪上本町〜西青山）",
    "08": "大阪線（東青山〜伊勢中川）",
    "09": "信貴線",
    "10": "名古屋線",
    "11": "湯の山線",
    "12": "鈴鹿線",
    "13": "山田線",
    "14": "鳥羽線",
    "15": "志摩線",
    "16": "南大阪線",
    "17": "吉野線",
    "18": "道明寺線",
    "19": "長野線",
    "20": "御所線",
}

BASE_URL = "https://tid.kintetsu.co.jp/LocationWeb/trainlocationinfoweb{}.html"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

notified_trains = set()


def get_line_stations(soup):
  stations = []
  for tag in soup.find_all(class_="station-name"):
    name = tag.get_text(strip=True).replace("\u3000", "").replace(" ", "")
    if name and name not in stations:
      stations.append(name)
  return {i + 1: name for i, name in enumerate(stations)}


def resolve_train_location(box, stations_dict):
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
    location_name = f"{st1} 〜 {st2} 間"

  return location_name, cell_num


def parse_delay_text(box):
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


def send_discord_embed(
    line_name, direction, dest, location_name, delay_text, line_code
):
  if not DISCORD_WEBHOOK_URL:
    return

  web_url = BASE_URL.format(line_code)
  now_str = time.strftime("%Y/%m/%d %H:%M:%S")

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
    print(f"Discord送信エラー: {e}")


def scan_once():
  for line_code, line_name in TARGET_LINES.items():
    url = BASE_URL.format(line_code)
    try:
      res = requests.get(url, headers=HEADERS, timeout=10)
      if res.status_code != 200:
        continue
      res.encoding = res.apparent_encoding
    except Exception:
      continue

    soup = BeautifulSoup(res.text, "html.parser")
    stations = get_line_stations(soup)
    train_boxes = soup.find_all(
        "div", class_=["train-info-up", "train-info-down"]
    )

    for box in train_boxes:
      direction = "上り" if "train-info-up" in box.get("class", []) else "下り"
      dest_tag = box.find("div", class_="train-destination-text")
      destination = dest_tag.get_text(strip=True) if dest_tag else ""

      img_tag = box.find("img")
      img_src = img_tag.get("src", "") if img_tag else ""

      delay_text = parse_delay_text(box)

      # 貸切判定
      is_kashikiri = (
          "09.png" in img_src
          or "貸切" in destination
          or "貸切" in box.get_text()
      )

      if is_kashikiri:
        location_name, cell_num = resolve_train_location(box, stations)
        train_key = f"{line_code}_{direction}_{destination}_{cell_num}"

        if train_key not in notified_trains:
          notified_trains.add(train_key)
          print(
              f"【貸切検知】{line_name} / {direction} / {location_name} /"
              f" {delay_text}"
          )
          send_discord_embed(
              line_name,
              direction,
              destination,
              location_name,
              delay_text,
              line_code,
          )


if __name__ == "__main__":

  # 1分おきに5回チェック（GitHub Actionsで5分バトンタッチ）
  for i in range(5):
    print(f"[{time.strftime('%H:%M:%S')}] スキャン実施 ({i+1}/5)")
    scan_once()
    if i < 4:
      time.sleep(60)
