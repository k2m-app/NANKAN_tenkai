import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
import numpy as np
import re
import requests
import time
import unicodedata
from datetime import datetime

# =====================================================================
# 南関4場の競馬場別・距離別 前半3F基準タイム（秒）
# 砂の深さや馬場形態の違いにより、同じ距離でも前半3Fの平均速度が異なる。
# 値が小さいほど「時計の出やすい（速い）馬場」であることを意味する。
# ※ これは直近1年程度の南関開催データから推定した概算値です。
#    より正確なデータがあれば差し替えてください。
# =====================================================================
TRACK_DISTANCE_BASE_3F = {
    "浦和": {
        1200: 36.5, 1300: 37.5, 1400: 38.5, 1500: 39.5, 1600: 40.0, 2000: 41.0,
    },
    "船橋": {
        1000: 35.5, 1200: 36.0, 1400: 38.0, 1500: 39.0, 1600: 39.5, 1800: 40.0,
    },
    "大井": {
        1000: 36.0, 1200: 36.5, 1400: 38.5, 1500: 39.5, 1600: 40.0, 1800: 40.5, 2000: 41.5, 2400: 42.5,
    },
    "川崎": {
        900: 35.5, 1200: 36.5, 1400: 38.0, 1500: 39.0, 1600: 39.5, 2000: 41.0, 2100: 41.5,
    },
}

# 全場の全距離を合算したグローバルデフォルト（該当なし時に使用）
GLOBAL_DEFAULT_3F = {
    900: 35.5, 1000: 35.8, 1200: 36.5, 1300: 37.5, 1400: 38.3,
    1500: 39.3, 1600: 39.8, 1800: 40.3, 2000: 41.2, 2100: 41.5, 2400: 42.5,
}


def get_base_3f(track_name, distance):
    """競馬場名と距離から基準前半3Fを返す"""
    # 競馬場名の正規化（「 」除去など）
    track_name = track_name.strip() if track_name else ""
    
    if track_name in TRACK_DISTANCE_BASE_3F:
        table = TRACK_DISTANCE_BASE_3F[track_name]
        if distance in table:
            return table[distance]
        # 最も近い距離にフォールバック
        closest = min(table.keys(), key=lambda d: abs(d - distance))
        return table[closest]
    
    # 競馬場名が一致しない場合はグローバルデフォルト
    if distance in GLOBAL_DEFAULT_3F:
        return GLOBAL_DEFAULT_3F[distance]
    closest = min(GLOBAL_DEFAULT_3F.keys(), key=lambda d: abs(d - distance))
    return GLOBAL_DEFAULT_3F[closest]


def parse_track_and_distance(kyori_text):
    """'ダ1400m良' のような文字列から競馬場名(なし)と距離(int)を抽出する"""
    if pd.isna(kyori_text):
        return None, np.nan
    m = re.search(r"(\d{3,4})", str(kyori_text))
    dist = int(m.group(1)) if m else np.nan
    return None, dist


def parse_track_from_date_loc(date_loc_text):
    """'2026/2/23 浦和' のような文字列から競馬場名を抽出する"""
    if pd.isna(date_loc_text):
        return ""
    # 南関4場
    for name in ["浦和", "船橋", "大井", "川崎"]:
        if name in str(date_loc_text):
            return name
            
    # JRAの主要競馬場
    for name in ["東京", "中山", "京都", "阪神", "新潟", "福島", "中京", "小倉", "札幌", "函館"]:
        if name in str(date_loc_text):
            return f"JRA_{name}"
            
    # それ以外は他地方として扱う（地名が含まれていればそれを抽出できればベストだが、ここでは「その他地方」フラグとして名前を適当に抽出）
    parts = str(date_loc_text).split()
    if len(parts) >= 2:
        return parts[1] # '盛岡' などの文字列
        
    return "Other"


class KeibaBookScraper:
    def __init__(self, login_id, password):
        self.login_id = login_id
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        self.base_url = "https://s.keibabook.co.jp"
        self.is_logged_in = False

    def login(self):
        """競馬ブックへ自動ログインする"""
        if not self.login_id or not self.password:
            return False
            
        try:
            res = self.session.get(f"{self.base_url}/login/login")
            soup = BeautifulSoup(res.text, "html.parser")
            
            token_meta = soup.find("meta", {"name": "csrf-token"})
            token = token_meta["content"] if token_meta else ""
            
            form = soup.find("form")
            if not form:
                return False
                
            action = form.get("action", f"{self.base_url}/login")
            if not action.startswith("http"):
                action = self.base_url + action
                
            payload = {}
            for inp in form.find_all("input"):
                name = inp.get("name")
                if not name: continue
                if inp.get("type") in ["hidden"]:
                    payload[name] = inp.get("value", "")
                elif inp.get("type") in ["text", "email", "tel"]:
                    payload[name] = self.login_id
                elif inp.get("type") == "password":
                    payload[name] = self.password
            
            if "login_id" not in payload and "email" not in payload:
                payload["login_id"] = self.login_id
                payload["password"] = self.password
                payload["_token"] = token
                
            res_post = self.session.post(action, data=payload)
            self.is_logged_in = "login" not in res_post.url
            return self.is_logged_in
            
        except Exception as e:
            st.error(f"ログインエラー: {e}")
            return False

    def get_horses_from_syutuba(self, race_url):
        """出馬表URLから各出走馬のURL一覧を取得する"""
        res = self.session.get(race_url)
        soup = BeautifulSoup(res.text, "html.parser")
        horses = []
        gate = 1
        seen_urls = set()
        
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"/db/uma/\d+(/top|/seiseki|/?)$", href):
                name = a.text.strip()
                if not name or len(name) > 20 or re.match(r"^\d+$", name): continue 
                
                match = re.search(r"/db/uma/(\d+)", href)
                if match:
                    horse_id = match.group(1)
                    full_url = f"{self.base_url}/db/uma/{horse_id}/top"
                    if full_url not in seen_urls:
                        horses.append({"gate_num": gate, "name": name, "url": full_url})
                        seen_urls.add(full_url)
                        gate += 1
                        
        return horses

    def get_race_info(self, race_url):
        """出馬表URLから今回のレースの距離と競馬場名を推定する"""
        res = self.session.get(race_url)
        soup = BeautifulSoup(res.text, "html.parser")
        
        track_name = ""
        distance = np.nan
        
        # .racetitle_sub の中身を取得
        racetitle_sub = soup.find("div", class_="racetitle_sub")
        if racetitle_sub:
            page_text = racetitle_sub.get_text()
            for name in ["浦和", "船橋", "大井", "川崎"]:
                if name in page_text:
                    track_name = name
                    break
            
            # 1400m などを抽出
            dist_match = re.search(r"(\d{3,4})m", page_text)
            if dist_match:
                distance = int(dist_match.group(1))
                
        # もし見つからなかった場合のために全体からも探す（フォールバック）
        if not track_name:
            page_text = soup.get_text()
            for name in ["浦和", "船橋", "大井", "川崎"]:
                if name in page_text:
                    track_name = name
                    break
        if pd.isna(distance):
            page_text = soup.get_text()
            dist_match = re.search(r"ダ(\d{3,4})m", page_text)
            if dist_match:
                distance = int(dist_match.group(1))
            
        return track_name, distance

    def get_horse_seiseki(self, horse_url):
        """馬の個別ページから過去成績テーブルを全てスクレイピングしてDataFrame化する"""
        res = self.session.get(horse_url)
        soup = BeautifulSoup(res.text, "html.parser")
        seiseki_list = []
        
        for div in soup.find_all("div", class_="uma_seiseki"):
            try:
                date_span = div.find("span", class_="negahi")
                cyakujun_span = div.find("span", class_=re.compile(r"cyakujun"))
                
                if not (date_span and cyakujun_span): continue
                
                date_loc = date_span.text.strip().replace("&nbsp;", " ")
                
                cyakujun_txt = cyakujun_span.text.strip()
                cyakujun_match = re.search(r"\d+", cyakujun_txt)
                cyakujun = float(cyakujun_match.group()) if cyakujun_match else np.nan
                
                kyori_span = div.find("span", class_="kyori")
                kyori = kyori_span.text.strip() if kyori_span else np.nan
                
                cyakusa_span = div.find("span", class_="cyakusa")
                margin = 0.0
                if cyakusa_span:
                    cyakusa_txt = cyakusa_span.text.strip()
                    if cyakusa_txt in ["アタマ", "クビ", "ハナ", "同着"]:
                        margin = 0.0
                    elif "大差" in cyakusa_txt:
                        margin = 5.0
                    else:
                        m_match = re.search(r"[\d\.]+", cyakusa_txt)
                        if m_match: margin = float(m_match.group())

                agari_span = div.find("span", class_="agari")
                first_3f = np.nan
                if agari_span:
                    agari_txt = agari_span.text.strip()
                    f3_match = re.search(r"[a-zA-Z]?(\d+\.\d+)-\d+\.\d+", agari_txt)
                    if f3_match: first_3f = float(f3_match.group(1))

                tosu_span = div.find("span", class_="tosu")
                tosu = np.nan
                if tosu_span:
                    t_match = re.search(r"\d+", tosu_span.text)
                    if t_match: tosu = float(t_match.group())

                ninki_span = div.find("span", class_="ninki")
                ninki = np.nan
                if ninki_span:
                    ni_match = re.search(r"\d+", ninki_span.text)
                    if ni_match: ninki = float(ni_match.group())

                tuka_ul = div.find("ul", class_="tuka")
                first_corner = np.nan
                avg_corner_pos = np.nan
                if tuka_ul:
                    positions = []
                    for li in tuka_ul.find_all("li"):
                        txt = li.text.strip()
                        if txt and txt not in ["　", ""]:
                            txt = unicodedata.normalize('NFKC', txt)
                            pos_m = re.search(r"\d+", txt)
                            if pos_m: positions.append(int(pos_m.group()))
                    if positions:
                        first_corner = float(positions[0])
                        avg_corner_pos = sum(positions) / len(positions)
                        
                gate_span = div.find("span", class_="gate")
                past_gate_num = np.nan
                if gate_span:
                    g_m = re.search(r"\d+", gate_span.text)
                    if g_m: past_gate_num = float(g_m.group())

                # 過去レースの競馬場名と距離を抽出
                past_track = parse_track_from_date_loc(date_loc)
                _, past_distance = parse_track_and_distance(kyori)
                        
                seiseki_list.append({
                    "date_loc": date_loc,
                    "kyori": kyori,
                    "past_track": past_track,
                    "past_distance": past_distance,
                    "finish_pos": cyakujun,
                    "margin": margin,
                    "first_3f": first_3f,
                    "tosu": tosu,
                    "ninki": ninki,
                    "first_corner": first_corner,
                    "avg_corner_pos": avg_corner_pos,
                    "past_gate_num": past_gate_num
                })
                
                if len(seiseki_list) >= 10:
                    break
            except Exception as e:
                continue
                
        return pd.DataFrame(seiseki_list)


def clean_and_fill_missing_data(df):
    """欠損値補完処理"""
    if df.empty: return df
    
    cols_to_fill = ['finish_pos', 'margin', 'first_3f_normalized', 'first_corner', 'avg_corner_pos', 'gate_num']
    for col in cols_to_fill:
        if col in df.columns:
            mean_val = df[col].mean()
            if pd.isna(mean_val): mean_val = 0.0
            df[col] = df[col].fillna(mean_val)
            
    return df


def normalize_first_3f(raw_3f, past_track, past_distance, current_track, current_distance):
    """
    前半3Fタイムを正規化する。
    1) 過去レースの競馬場＆距離の基準3Fとの差分（偏差）を算出
    2) その偏差を今回レースの競馬場＆距離の基準3Fに加算して投影
    → 異なる距離・異なる競馬場のタイムを「今回の条件に換算した3F」として比較可能にする
    """
    if pd.isna(raw_3f) or pd.isna(past_distance) or pd.isna(current_distance):
        return raw_3f
    
    past_base = get_base_3f(past_track, past_distance)
    current_base = get_base_3f(current_track, current_distance)
    
    # 偏差 = 実測値 - 過去レースの基準値（マイナス = 基準より速い）
    deviation = raw_3f - past_base
    
    # 今回の基準に投影
    normalized = current_base + deviation
    return normalized


def apply_distance_change_bonus(first_corner, past_distance, current_distance):
    """
    距離変動による前への行きやすさの補正。
    距離延長 → テンは楽になる → first_corner を手前（小さい値）に補正
    距離短縮 → テンは厳しくなる → first_corner を後ろ（大きい値）に補正
    """
    if pd.isna(first_corner) or pd.isna(past_distance) or pd.isna(current_distance):
        return first_corner
    
    diff = current_distance - past_distance  # 正=延長, 負=短縮
    
    # 200mの変動につき ±0.3 番手の補正（概算）
    adjustment = -(diff / 200.0) * 0.3
    
    return max(1.0, first_corner + adjustment)


def aggregate_horse_stats(horse_name, current_gate, current_tosu, current_track, current_distance, seiseki_df):
    """対象馬の過去成績から、距離・馬場補正付きの代表値を算出する（最大10走・枠別・ウェイト付き）"""
    if seiseki_df.empty:
        return {
            "horse_name": horse_name,
            "gate_num": current_gate,
            "finish_pos": np.nan,
            "margin": np.nan,
            "first_3f_normalized": np.nan,
            "first_corner": np.nan,
            "avg_corner_pos": np.nan,
            "frame_type": "unknown",
            "running_style": ""
        }
    
    def get_frame_type(gate, tosu):
        if pd.isna(gate) or pd.isna(tosu) or tosu <= 0:
            return "middle"
        if gate <= tosu / 3.0:
            return "inner"
        elif gate > (tosu * 2.0) / 3.0:
            return "outer"
        else:
            return "middle"

    current_frame = get_frame_type(current_gate, current_tosu)
    
    seiseki_df = seiseki_df.head(10).copy()
    
    # --- 脚質・特徴の分析（逃げないとダメな馬か） ---
    running_style = ""
    valid_races = seiseki_df.dropna(subset=['finish_pos', 'first_corner'])
    if not valid_races.empty:
        # 3着以内を好走と定義
        good_races = valid_races[valid_races['finish_pos'] <= 3]
        bad_races = valid_races[valid_races['finish_pos'] > 3]
        
        if len(good_races) > 0:
            good_corner_avg = good_races['first_corner'].mean()
            good_corner_max = good_races['first_corner'].max() # 過去の好走時で一番後ろだった位置
            
            # 【厳格化】逃げ必須の判定：好走時はほぼハナ（平均1.3以下）かつ、2番手以降での好走経験がなく、控えた時に大敗している
            if good_corner_avg <= 1.3 and good_corner_max <= 1.5:
                if len(bad_races) > 0 and bad_races['first_corner'].mean() >= 2.5:
                    running_style = "逃げ必須(砂被りNG)"
                elif len(bad_races) == 0:
                    # 負けたことがない、あるいは常に逃げて勝っている馬
                    running_style = "逃げ必須(砂被りNG)"
            
            # 【緩和】2番手でも馬券に絡めている馬は「生粋の先行馬」とする
            elif good_corner_avg <= 2.5:
                if running_style == "":
                    running_style = "生粋の先行馬"

    # --- 他場/JRA補正の適用 ---
    # 南関以外の地方やJRAからの転入・遠征の場合、南関基準と比較してペース予測が変わる。
    # JRAのテンの速さは南関より速いため、JRAで中団(5番手)なら南関では先行(3,4番手)できる傾向。
    def adjust_corner_for_track(corner, track):
        if pd.isna(corner): return corner
        t_str = str(track)
        if t_str in ["浦和", "船橋", "大井", "川崎", ""]:
            return corner
        elif t_str.startswith("JRA_"):
            # JRAのレースは南関に来ると1.0〜1.5番手前に行ける補正
            return max(1.0, corner - 1.2)
        else:
            # 他地方（門別、園田、高知など）のレースは、相手関係によるが一般に南関よりややテンが遅いか同等。
            # 少し厳し目（+0.5番手後方）に見積もる。※門別は例外的に速いが今回は一律処理
            return corner + 0.5
            
    seiseki_df['first_corner_track_adj'] = seiseki_df.apply(
        lambda r: adjust_corner_for_track(r.get('first_corner'), r.get('past_track')), axis=1
    )

    # --- 前半3Fの正規化と距離変動ボーナスの適用 ---
    seiseki_df['first_3f_normalized'] = seiseki_df.apply(
        lambda r: normalize_first_3f(
            r.get('first_3f'), r.get('past_track', ''), r.get('past_distance', np.nan),
            current_track, current_distance
        ), axis=1
    )
    
    seiseki_df['first_corner_adj'] = seiseki_df.apply(
        lambda r: apply_distance_change_bonus(
            r.get('first_corner_track_adj'), r.get('past_distance', np.nan), current_distance
        ), axis=1
    )
    
    # --- ウェイト算出 ---
    weights = []
    for i, row in seiseki_df.iterrows():
        w = 1.0
        # 直近3走のウェイト
        if i < 3:
            w += 0.5
        # 好走実績のウェイト
        if pd.notna(row.get('finish_pos')) and row['finish_pos'] <= 2:
            w += 1.0
        # 人気以上の激走ウェイト
        if pd.notna(row.get('finish_pos')) and pd.notna(row.get('ninki')):
            if row['finish_pos'] < row['ninki']:
                w += 1.0
                
        # 【追加】他競馬場・JRAの過去データは情報信頼度を下げる（ウェイト割引）
        t_str = str(row.get('past_track', ''))
        if t_str not in ["浦和", "船橋", "大井", "川崎", ""]:
            w *= 0.6  # 南関以外の走りは重要度を4割引き
            
        weights.append(w)
        
    seiseki_df['weight'] = weights
    seiseki_df['frame_type'] = seiseki_df.apply(lambda r: get_frame_type(r.get('past_gate_num'), r.get('tosu')), axis=1)
    
    # --- 【大穴逃げ警戒】の判定（伏兵の単騎逃げによる波乱実績） ---
    is_alert_runner = False
    alert_reason = ""
    # 直近成績の中で「人気薄（5人気以降など）にも関わらず逃げて（初角2番手以内）好走実績あり」を探す
    for _, row in seiseki_df.iterrows():
        f_pos = row.get('finish_pos')
        f_cor = row.get('first_corner')
        ninki = row.get('ninki')
        
        if pd.notna(f_pos) and pd.notna(f_cor) and pd.notna(ninki):
            # 6番人気以下 かつ 最初のコーナー2番手以内 かつ 3着以内
            if ninki >= 6 and f_cor <= 2.0 and f_pos <= 3:
                is_alert_runner = True
                alert_reason = f"過去に{int(ninki)}番人気で逃げて{int(f_pos)}着に粘り込む波乱実績あり"
                break # 1つでもあればフラグを立てて終了
    
    # --- 【内枠快速馬の好走実績】の判定 ---
    # 過去に逃げた経験があるか、内枠時にポジションを前(3番手以内)につけて、人気より着順が2つ以上上振れたor3着以内
    has_inner_push_history = False
    for _, row in seiseki_df.iterrows():
        f_pos = row.get('finish_pos')
        f_cor = row.get('first_corner')
        ninki = row.get('ninki')
        p_gate = row.get('past_gate_num')
        tosu = row.get('tosu')
        
        if pd.notna(f_cor):
            # 1. 過去に逃げた経験がある (ここでは初角1番手を通ったことがあるか)
            if f_cor <= 1.5:
                has_inner_push_history = True
                break
                
            # 2. 内枠の時に前につけて好走したか
            if pd.notna(p_gate) and pd.notna(tosu) and tosu > 0 and pd.notna(f_pos) and pd.notna(ninki):
                is_inner_past = (p_gate <= tosu / 3.0)
                if is_inner_past and f_cor <= 3.0:
                    if f_pos <= 3 or f_pos <= ninki - 2:
                        has_inner_push_history = True
                        break
    
    def weighted_avg(df, col):
        valid = df.dropna(subset=[col])
        if valid.empty: return np.nan
        return np.average(valid[col], weights=valid['weight'])

    frame_df = seiseki_df[seiseki_df['frame_type'] == current_frame]
    
    if frame_df.empty or frame_df['first_3f_normalized'].isna().all():
        frame_df = seiseki_df
        
    first_3f_norm = weighted_avg(frame_df, 'first_3f_normalized')
    first_corner = weighted_avg(frame_df, 'first_corner_adj')
    
    finish_pos = weighted_avg(seiseki_df, 'finish_pos')
    margin = weighted_avg(seiseki_df, 'margin')
    avg_corner_pos = weighted_avg(seiseki_df, 'avg_corner_pos')
    
    return {
        "horse_name": horse_name,
        "gate_num": current_gate,
        "finish_pos": finish_pos,
        "margin": margin,
        "first_3f_normalized": first_3f_norm,
        "first_corner": first_corner,
        "avg_corner_pos": avg_corner_pos,
        "frame_type": current_frame,
        "running_style": running_style,
        "is_alert_runner": is_alert_runner,
        "alert_reason": alert_reason,
        "has_inner_push_history": has_inner_push_history
    }


def get_race_url_from_base(base_url, target_race_num):
    """
    ユーザーが入力した1レース分のURLを基準に、別のレース番号のURLを生成する。
    例 format: https://s.keibabook.co.jp/chihou/syutuba/2026021301010223
    後ろから5文字目、6文字目がレース番号（この例だと 01）
    """
    # URL末尾のID部分（数字の連続）を取得
    match = re.search(r'/(\d+)$', base_url.strip())
    if not match:
        return base_url # URL形式が想定外ならそのまま返す
        
    id_str = match.group(1)
    if len(id_str) >= 6:
        # 後ろから5,6文字目を置換（例: id_strが16桁なら、インデックスは -6番目と-5番目）
        # Pythonの文字列スライスで組み立てる
        prefix = id_str[:-6]
        # target_race_num を0埋め2桁に
        race_str = f"{target_race_num:02d}"
        suffix = id_str[-4:]
        
        new_id = f"{prefix}{race_str}{suffix}"
        return base_url.replace(id_str, new_id)
    
    return base_url


def run_prediction_for_race(scraper, race_url, target_race_num=None):
    """1レース分のデータ収集と予想実行処理を関数化"""
    try:
        # 1. 今回のレース情報を取得（競馬場名・距離）
        current_track, current_distance = scraper.get_race_info(race_url)
        if pd.isna(current_distance) or not current_track:
            return None, "出馬表ページから距離または競馬場を検出できませんでした（対象レースが存在しないか、URL無効の可能性があります）。"
            
        header_text = f"📍 {current_track} {target_race_num}R ダ{int(current_distance)}m" if target_race_num else f"📍 今回のレース: {current_track} ダ{int(current_distance)}m"
        st.info(f"{header_text} | 基準前半3F: **{get_base_3f(current_track, current_distance):.1f}秒**")
        
        # 2. 出馬表から各馬のURLを取得
        horses = scraper.get_horses_from_syutuba(race_url)
        if not horses:
            return None, "出馬表から出走馬情報を取得できませんでした。"
            
        progress_bar = st.progress(0, text=f"{header_text} - 過去成績データを解析中...")
        
        # 3. 各馬の成績取得・集計
        aggregated_data = []
        for i, h in enumerate(horses):
            horse_name = h["name"]
            horse_url = h["url"]
            
            time.sleep(0.3) # 負荷軽減
            seiseki_df = scraper.get_horse_seiseki(horse_url)
            
            current_tosu = len(horses)
            stats_dict = aggregate_horse_stats(
                horse_name, h["gate_num"], current_tosu,
                current_track, current_distance, seiseki_df
            )
            aggregated_data.append(stats_dict)
            
            progress_bar.progress((i + 1) / len(horses), text=f"{header_text} - 解析中... ({i+1}/{len(horses)}頭)")
            
        progress_bar.empty()
        
        df_raw = pd.DataFrame(aggregated_data)
        df_cleaned = clean_and_fill_missing_data(df_raw)
        df_scored = sort_by_pace(df_cleaned)
        
        return df_scored, None
        
    except Exception as e:
        return None, f"エラーが発生しました: {e}"


def sort_by_pace(df):
    """展開予想に必要な要素（最初のコーナー位置、前半3F）でデータをソートする"""
    if df.empty: return df
    
    temp_df = df.copy()
    if 'first_3f_normalized' not in temp_df.columns:
        temp_df['first_3f_normalized'] = np.nan
    if 'first_corner' not in temp_df.columns:
        temp_df['first_corner'] = np.nan
        
    return temp_df.sort_values(by=['first_corner', 'first_3f_normalized'], ascending=[True, True]).reset_index(drop=True)


def generate_race_formation(df):
    """
    first_corner と first_3f_normalized から、レースの想定展開（隊列）を生成する。
    予想される並び順を馬番の丸数字で「←逃げ①②　③④...」のように表現。
    """
    if df.empty or 'first_corner' not in df.columns:
        return "データ不足のため隊列は生成できません。"
        
    circle_nums = {
        1:"①", 2:"②", 3:"③", 4:"④", 5:"⑤", 6:"⑥", 7:"⑦", 8:"⑧", 9:"⑨", 10:"⑩",
        11:"⑪", 12:"⑫", 13:"⑬", 14:"⑭", 15:"⑮", 16:"⑯", 17:"⑰", 18:"⑱"
    }
    
    pace_col = 'first_3f_normalized' if 'first_3f_normalized' in df.columns else 'first_3f'
    
    temp_df = df.copy()
    temp_df['first_corner'] = temp_df['first_corner'].fillna(99)
    temp_df[pace_col] = temp_df[pace_col].fillna(99.0)
    
    sorted_df = temp_df.sort_values(by=['first_corner', pace_col], ascending=[True, True])
    
    formation_groups = []
    current_group = []
    current_corner = None
    
    for _, row in sorted_df.iterrows():
        g_num = int(row['gate_num']) if pd.notna(row['gate_num']) else 0
        c_mark = circle_nums.get(g_num, f"({g_num})")
        c_pos = row['first_corner']
        
        if current_corner is None:
            current_corner = c_pos
            current_group.append(c_mark)
        elif abs(c_pos - current_corner) <= 1.5:
            current_group.append(c_mark)
        else:
            formation_groups.append("".join(current_group))
            current_corner = c_pos
            current_group = [c_mark]
            
    if current_group:
        formation_groups.append("".join(current_group))
        
    return "←逃げ " + "　".join(formation_groups)


def generate_pace_prediction_text(df, current_base):
    """
    データからペース予想と逃げ馬の解説テキストを生成する。
    """
    if df.empty or 'first_corner' not in df.columns:
        return "データ不足のためペース予想は生成できません。"
        
    circle_nums = {
        1:"①", 2:"②", 3:"③", 4:"④", 5:"⑤", 6:"⑥", 7:"⑦", 8:"⑧", 9:"⑨", 10:"⑩",
        11:"⑪", 12:"⑫", 13:"⑬", 14:"⑭", 15:"⑮", 16:"⑯", 17:"⑰", 18:"⑱"
    }

    pace_col = 'first_3f_normalized' if 'first_3f_normalized' in df.columns else 'first_3f'
    
    # first_cornerとペースが取得できている馬に絞る
    temp_df = df.dropna(subset=['first_corner', pace_col]).copy()
    if temp_df.empty:
        return "データ不足のためペース予想は生成できません。"

    has_style = 'running_style' in temp_df.columns
    must_lead_horses = temp_df[temp_df['running_style'].str.contains("逃げ必須", na=False)] if has_style else pd.DataFrame()
    wait_horses = temp_df[temp_df['running_style'].str.contains("生粋の先行馬", na=False)] if has_style else pd.DataFrame()

    # 逃げ候補（first_corner が 2.5 以下、もしくは上位2頭）
    # テンの速さランキングを計算しておく（順位判定用）
    temp_df['pace_rank'] = temp_df[pace_col].rank(ascending=True, method='min')
    
    # 【追加】内枠でテンが非常に速い馬を逃げ候補に強制追加（過去に逃げ実績or内枠好走実績がある馬のみ）
    if 'has_inner_push_history' in temp_df.columns:
        inner_fast_horses = temp_df[(temp_df['gate_num'] <= len(df) / 2.0) & 
                                    (temp_df['pace_rank'] <= 2) & 
                                    (temp_df['first_corner'] <= 4.0) &
                                    (temp_df['has_inner_push_history'] == True)]
    else:
        inner_fast_horses = pd.DataFrame()
    
    front_runners = temp_df[temp_df['first_corner'] <= 2.5]
    
    # 重複を排除して統合
    front_runners = pd.concat([front_runners, inner_fast_horses]).drop_duplicates(subset=['horse_name']).sort_values(by='gate_num')

    if len(front_runners) == 0:
        front_runners = temp_df.head(2)
        
    front_runner_nums = [circle_nums.get(int(row['gate_num']), f"({int(row['gate_num'])})") for _, row in front_runners.iterrows()]
    
    if len(front_runners) >= 2:
        # 先行争いする馬のタイム差をチェック
        times = front_runners[pace_col].values
        time_diff = max(times) - min(times)
        avg_front_time = np.mean(times)
        
        # 枠の並びによる追加チェック
        gates = sorted([int(r['gate_num']) for _, r in front_runners.iterrows()])
        adjacent_front = any([gates[i+1] - gates[i] <= 1 for i in range(len(gates)-1)]) if len(gates) >= 2 else False
        
        # 【追加】内枠のテン速い馬 vs 外枠のテン遅い逃げ馬による自滅ハイペース判定
        fastest_front = front_runners.loc[front_runners[pace_col].idxmin()]
        fastest_gate = int(fastest_front['gate_num'])
        fastest_num = circle_nums.get(fastest_gate, f"({fastest_gate})")
        
        outer_challengers = front_runners[(front_runners['gate_num'] > fastest_gate) & (front_runners['horse_name'] != fastest_front['horse_name'])]
        
        has_outer_crush = False
        outer_crush_nums = []
        if fastest_gate <= len(df) / 2.0 and len(outer_challengers) > 0:
            # 内の最速馬に対して、外の逃げ馬が明確に遅い（0.2秒以上）にも関わらずハナを主張せざるを得ない構成
            if outer_challengers[pace_col].min() > fastest_front[pace_col] + 0.2:
                has_outer_crush = True
                outer_crush_nums = [circle_nums.get(int(r['gate_num']), f"({int(r['gate_num'])})") for _, r in outer_challengers.iterrows()]

        if has_outer_crush:
            pace = "ハイペース（外の先行馬崩れ警戒）"
            reason = f"内枠にテンの速い{fastest_num}がおり、外枠の{'・'.join(outer_crush_nums)}もハナを切りたい構成です。外の馬は強引に競りかける形になりますが、内の{fastest_num}の方がテンの時計が速いため、外の同型馬たちは外回りを強いられ無駄脚を使って共倒れするリスク（ハイペースの差し展開や{fastest_num}の単騎逃げ残り）が極めて高いです。"
        elif avg_front_time < current_base - 0.2 and time_diff <= 0.6:
            pace = "ハイペース"
            if adjacent_front:
                reason = f"{'・'.join(front_runner_nums)}が隣り合った枠に入っており、テンの速さも近いため序盤から熾烈なハナ争いになり、ハイペースになる懸念が強いです。"
            else:
                reason = f"{'・'.join(front_runner_nums)}が逃げると強いタイプで、テンの速さも近しいため熾烈に争ってペースが上がる懸念あり。"
        elif avg_front_time > current_base + 0.3:
            pace = "スローペース"
            reason = f"{'・'.join(front_runner_nums)}が逃げ争いになりそうですが、テンの時計はそこまで速くなく、無理せず番手に切り替えて落ち着く見通しです。"
        else:
            if time_diff <= 0.6:
                pace = "平均〜ややハイペース"
                reason = f"{'・'.join(front_runner_nums)}が先行争いを形成。ある程度前がやり合って、ペースは平均かやや速めになる見通しです。"
            else:
                pace = "平均ペース"
                reason = f"{front_runner_nums[0]}がハナを切り、{'・'.join(front_runner_nums[1:])}が続く展開。無理な競り合いにはならず、平均的な流れになりそうですが、隊列次第で前後する可能性があります。"
                
                # 自分より外1〜2個となりに控えられる（番手競馬できる）先行馬がいるなら落ち着く
                fastest_front_gate = int(front_runners.iloc[0]['gate_num'])
                outer_wait_horses = wait_horses[(wait_horses['gate_num'] > fastest_front_gate) & (wait_horses['gate_num'] <= fastest_front_gate + 2)]
                if len(outer_wait_horses) > 0 and not adjacent_front:
                    outer_nums = [circle_nums.get(int(r['gate_num']), f"({int(r['gate_num'])})") for _, r in outer_wait_horses.iterrows()]
                    pace = "スロー〜平均ペース"
                    reason = f"{front_runner_nums[0]}がハナを主張しますが、すぐ外側の枠の{'・'.join(outer_nums)}が無理に競りかけず番手で競馬ができるタイプのため、隊列がすんなり決まってペースが落ち着く見通しです。"
    else:
        # 逃げ馬が1頭の場合
        front_runner = front_runner_nums[0]
        time = front_runners[pace_col].values[0]
        if time < current_base - 0.3:
            pace = "ハイペース"
            reason = f"{front_runner}の単騎逃げが濃厚ですが、他馬よりテンのスピードが抜けて速いため、縦長でペースは上がりそうです。"
        elif time > current_base + 0.3:
            pace = "スローペース"
            reason = f"{front_runner}が楽に主導権を握れそうです。競りかける馬もおらず、無理せずマイペースでスローな展開になりそうです。"
        else:
            pace = "平均ペース"
            reason = f"{front_runner}が単騎で逃げる展開。後続も極端に競りかけることなく、平均的なペースに落ち着きそうです。"

    must_lead_text = ""
    if has_style and not must_lead_horses.empty:
        for _, row in must_lead_horses.iterrows():
            gate = int(row['gate_num'])
            horse_c = circle_nums.get(gate, f"({gate})")
            horse_time = row[pace_col]
            my_pace_rank = row['pace_rank']
            
            faster_horses = temp_df[temp_df[pace_col] < horse_time - 0.1]
            total_horses = len(df)
            
            # --- ここから枠や距離による「逃げられるかどうかの期待値」判定 ---
            is_inner = (gate == 1 or gate == 2) # 最内枠付近
            is_outer = (gate >= total_horses - 1) # 大外付近
            
            # 距離延長による前進気勢の確認（前走距離がわかっている馬全体から判定するのは難しいため、
            # まずは「テンの速さが上位3位以内か」または「極端な枠（内外）」を救済条件とする）
            can_push = (my_pace_rank <= 3) or is_inner or is_outer
            
            if not faster_horses.empty:
                faster_nums = [circle_nums.get(int(r['gate_num']), f"({int(r['gate_num'])})") for _, r in faster_horses.iterrows()]
                faster_str = "・".join(faster_nums[:2])
                
                if gate <= total_horses / 3.0: # 内枠（大きめ）
                    if can_push:
                        must_lead_text += f"\n\n※特注: {horse_c}は内枠に入り包まれるのを嫌う（揉まれ弱い）タイプです。テンの速さでは{faster_str}らに一歩譲りますが、枠の利（あるいは持ち前のスピード）を活かして強引にハナを奪いに行くと、そのまま残る可能性や全体のペースが乱れる要因になります。"
                    else:
                        must_lead_text += f"\n\n※特注: {horse_c}は揉まれ弱い逃げ必須タイプですが、今回は内目の中途半端な枠。テンの速さも{faster_str}らに劣り（全体の{int(my_pace_rank)}位タイ）、無理に主張できず馬群に沈み苦しい競馬になる可能性が高いです。"
                else:
                    if can_push:
                        must_lead_text += f"\n\n※特注: {horse_c}は逃げないと脆いタイプ。テンのスピードは{faster_str}らにやや勝てませんが、外枠から被されずに（または持ち前のスピードで）強引にハナを叩き切れれば展開が向いて残る目があります。"
                    else:
                        must_lead_text += f"\n\n※特注: {horse_c}は逃げないと脆いタイプですが、今回はテンの速さがメンバー中{int(my_pace_rank)}位タイと劣ります。外から被される展開になりやすく、自分の形を作れず厳しいレースになりそうです。"
            else:
                must_lead_text += f"\n\n※特注: {horse_c}は逃げ必須のタイプですが、今回メンバー中テンの速さが抜けて優勢（1位）。難なくハナを切れれば自分のペースに持ち込んで好走する可能性が高いです。"

    alert_text = ""
    # スローペースや逃げ馬不在の時に、過去に人気薄で逃げ残りした馬をピックアップ
    if 'is_alert_runner' in temp_df.columns:
        alert_horses = temp_df[temp_df['is_alert_runner'] == True]
        for _, row in alert_horses.iterrows():
            gate = int(row['gate_num'])
            horse_c = circle_nums.get(gate, f"({gate})")
            reason_str = row.get('alert_reason', '')
            # 普段は後ろにいる馬がしれっと逃げたケースなどへの警戒
            alert_text += f"\n\n🚨大穴・逃げ警戒: {horse_c}は{reason_str}。前に行く馬の手薄な構成なら、ノーマークの単騎逃げで波乱を演出する可能性があります。"

    return f"**【{pace}予想】**\n{reason}{must_lead_text}{alert_text}"


def main():
    st.set_page_config(page_title="南関競馬 展開予想AI", layout="wide", page_icon="🐎")
    st.title("🐎 南関競馬 展開予想AI（自動スクレイピング連携）")
    st.markdown("競馬ブックのサイトから自動で出走馬情報を取得し、全ての馬の過去実績から展開AIで予想を行います。")
    
    # --- secrets.toml からの自動読み込み ---
    secrets_login_id = ""
    secrets_password = ""
    try:
        secrets_login_id = st.secrets["keibabook"]["login_id"]
        secrets_password = st.secrets["keibabook"]["password"]
    except Exception:
        pass  # secrets.toml が未設定の場合は手入力にフォールバック
    
    with st.sidebar:
        st.header("🔑 競馬ブック ログイン情報")
        if secrets_login_id and secrets_login_id != "あなたのログインID":
            st.success("✅ secrets.toml からログイン情報を読み込みました。")
            login_id = secrets_login_id
            password = secrets_password
        else:
            st.info("※ `.streamlit/secrets.toml` にID/PWを設定すれば自動ログインできます。")
            login_id = st.text_input("ログインID (またはメール)")
            password = st.text_input("パスワード", type="password")
        
        
    st.markdown("### 🎯 予想対象レースの設定")
    
    st.markdown("当日のいずれか1レースの出馬表URLを入力してください。<br>（地方競馬トップ： https://s.keibabook.co.jp/chihou/top ）", unsafe_allow_html=True)
    base_race_url = st.text_input("出馬表URL", placeholder="例: https://s.keibabook.co.jp/chihou/syutuba/2026021301010223")
    
    st.markdown("#### 予想を出力するレースを選択")
    
    st.markdown("""
        <style>
        /* チェックボックスを横並びにするCSS */
        div.row-widget.stCheckbox { display: inline-block; margin-right: 15px; }
        </style>
    """, unsafe_allow_html=True)
    
    # 1〜12Rのチェックボックスを配置
    cols = st.columns(6)
    selected_races = []
    for i in range(1, 13):
        col_idx = (i - 1) % 6
        with cols[col_idx]:
            if st.checkbox(f"{i}R", value=(i==1)): # 1RだけデフォルトON
                selected_races.append(i)
                
    st.markdown("---")
    
    if st.button("データ収集＆展開予想を実行する 🚀", type="primary"):
        if not base_race_url:
            st.error("出馬表のURLを入力してください。")
            return
            
        if not selected_races:
            st.warning("予想を出力するレースを1つ以上選択してください。")
            return
        with st.spinner("スクレイピングを実行中... (各馬のページに順次アクセスします)"):
            scraper = KeibaBookScraper(login_id, password)
            if login_id and password:
                if scraper.login():
                    st.sidebar.success("ログイン成功！")
                else:
                    st.sidebar.warning("ログイン失敗（ゲストとして続行）")
            
            for r_num in selected_races:
                if len(selected_races) > 1:
                    st.markdown(f"## 🏁 {r_num}R の予想")
                    
                race_url = get_race_url_from_base(base_race_url, r_num)
                # デバッグ表示（本番環境では消してもOK）
                # st.caption(f"Generated URL: {race_url}")
                
                df_scored, error_msg = run_prediction_for_race(scraper, race_url, target_race_num=r_num)
                
                if error_msg:
                    st.error(error_msg)
                    if len(target_races) > 1:
                        st.divider()
                        time.sleep(1)
                    continue
                
                # --- 結果表示 ---
                display_cols = ['horse_name', 'gate_num', 'first_corner', 'first_3f_normalized', 'frame_type', 'running_style']
                cols_to_show = [c for c in display_cols if c in df_scored.columns]
                
                st.dataframe(
                    df_scored[cols_to_show].style
                        .format({'first_3f_normalized': '{:.1f}秒', 'first_corner': '{:.1f}'}),
                    use_container_width=True,
                    height=450
                )
                
                # 展開予想テキストと隊列図
                st.subheader("🏃‍♂️ 想定されるレース隊列と展開解説")
                formation_str = generate_race_formation(df_scored)
                st.info(f"**{formation_str}**")
                
                current_distance = float(scraper.get_race_info(race_url)[1]) if pd.notna(scraper.get_race_info(race_url)[1]) else 1400.0
                current_track_from_url = scraper.get_race_info(race_url)[0] if scraper.get_race_info(race_url)[0] else "大井"
                current_base = get_base_3f(current_track_from_url, current_distance)
                pace_text = generate_pace_prediction_text(df_scored, current_base)
                st.markdown(pace_text)
                
                with st.expander(f"{r_num}R 各馬の詳細データ（直近最大10走・内中外枠区分等）"):
                    st.dataframe(df_scored, use_container_width=True)
                    
                if len(selected_races) > 1:
                    st.divider()
                    time.sleep(1.5) # レース間のインターバル
                    
            st.success("全ての予想が完了しました！")

if __name__ == "__main__":
    main()
