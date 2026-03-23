import os
import threading
import sqlite3
from flask import Flask, jsonify
from dotenv import load_dotenv
from steam.client import SteamClient
from steam.enums import EPersonaState, EFriendRelationship

# 1. 加载配置 (仅读取账密)
load_dotenv()
STEAM_USER = os.getenv("STEAM_USERNAME")
STEAM_PASS = os.getenv("STEAM_PASSWORD")

app = Flask(__name__)
client = SteamClient()

DB_NAME = "steam_status.db"
LOGIN_KEY_FILE = "login_key.txt"
friends_cache = {}

# ==========================================
# 1. 数据库持久化逻辑
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS status_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    steam_id TEXT,
                    name TEXT,
                    state TEXT,
                    game_appid TEXT,
                    game_name TEXT,
                    rich_display TEXT,
                    group_size TEXT,
                    party_id TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    conn.commit()
    conn.close()

def log_to_db(status):
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''INSERT INTO status_log 
                     (steam_id, name, state, game_appid, game_name, rich_display, group_size, party_id) 
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (status['steam_id'], status['name'], status['state'], 
                   status['game_appid'], status['game_name'], 
                   status['rich_display'], status['group_size'], status['party_id']))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ 数据库写入失败: {e}")

# ==========================================
# 2. 解析玩家状态
# ==========================================
def parse_user_status(user):
    if not user: return None
    try:
        state_name = EPersonaState(user.state).name
        game_id = getattr(user, 'game_id', None)
        game_appid = str(game_id) if game_id else ""
        game_name = getattr(user, 'game_name', "") or ""
        
        rich_display, group_size, party_id = "", "", ""
        rp = getattr(user, 'rich_presence', {})
        if rp:
            rich_display = rp.get('steam_display', '')
            group_size = rp.get('steam_player_group_size', '')
            party_id = rp.get('steam_player_group', '')
            
        return {
            "steam_id": str(user.steam_id),
            "name": user.name or str(user.steam_id),
            "state": state_name,
            "game_appid": game_appid,
            "game_name": game_name,
            "rich_display": rich_display,
            "group_size": group_size,
            "party_id": party_id
        }
    except Exception as e:
        print(f"⚠️ 解析用户 {user.steam_id} 失败: {e}")
        return None

# ==========================================
# 3. Web API 接口
# ==========================================
@app.route('/api/friends', methods=['GET'])
def get_all_friends():
    final_data = []
    for steam_id, status in friends_cache.items():
        friend_data = dict(status)
        party_members = []
        if friend_data.get('party_id') and friend_data.get('game_appid'):
            for o_id, o_status in friends_cache.items():
                if (o_id != steam_id and 
                    o_status.get('party_id') == friend_data['party_id'] and 
                    o_status.get('game_appid') == friend_data['game_appid']):
                    party_members.append(o_status['name'])
        friend_data['party_members'] = party_members
        final_data.append(friend_data)
    return jsonify({"status": "success", "data": final_data})

# ==========================================
# 4. Steam 事件处理
# ==========================================
@client.on('login_key_callback')
def save_login_key(key):
    if key:
        with open(LOGIN_KEY_FILE, "w") as f:
            f.write(key)
        print(f"💾 登录令牌已保存到 {LOGIN_KEY_FILE}")

@client.on('logged_on')
def handle_logged_on():
    print("\n--- ✅ 成功登录 Steam 网络 ---")
    client.persona_state = EPersonaState.Online
    
    if not client.friends.ready:
        client.friends.wait_event('ready')
    
    print(f"👥 初始化 {len(client.friends)} 名联系人...")
    for friend in client.friends:
        if friend.relationship == EFriendRelationship.RequestRecipient:
            client.friends.add(friend.steam_id)
            continue
            
        status = parse_user_status(friend)
        if status:
            friends_cache[status['steam_id']] = status
            
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False), daemon=True).start()
    print("👀 监控已就绪。\n")

@client.friends.on('friend_invite')
def handle_friend_invite(user):
    print(f"💌 自动通过来自 {user.name or user.steam_id} 的请求")
    client.friends.add(user.steam_id)

@client.on('persona_state_updated')
def handle_state_change(user, _):
    if user.relationship != EFriendRelationship.Friend:
        return

    new_s = parse_user_status(user)
    if not new_s: return
    
    sid = new_s['steam_id']
    old_s = friends_cache.get(sid, {})
    
    if (new_s['state'] != old_s.get('state') or 
        new_s['game_appid'] != old_s.get('game_appid') or 
        new_s['rich_display'] != old_s.get('rich_display') or 
        new_s['party_id'] != old_s.get('party_id')):
        
        friends_cache[sid] = new_s
        log_to_db(new_s)
        
        g_info = f" | 🎮 {new_s['game_name']} ({new_s['rich_display']})" if new_s['game_name'] else ""
        print(f"[更新] {new_s['name']} -> {new_s['state']}{g_info}")

# ==========================================
# 5. 启动逻辑
# ==========================================
if __name__ == '__main__':
    if not STEAM_USER or not STEAM_PASS:
        print("❌ 错误: 请在 .env 配置 STEAM_USERNAME 和 STEAM_PASSWORD")
    else:
        init_db()
        
        if os.path.exists(LOGIN_KEY_FILE):
            with open(LOGIN_KEY_FILE, "r") as f:
                client.login_key = f.read().strip()
                client.username = STEAM_USER
                print("🔑 载入本地令牌...")

        try:
            # 依靠外部 proxychains4 处理网络
            result = client.cli_login(username=STEAM_USER, password=STEAM_PASS)
            if result == 1:
                client.run_forever()
            else:
                print(f"❌ 登录失败: {result}")
        except KeyboardInterrupt:
            print("\n🛑 已停止。")