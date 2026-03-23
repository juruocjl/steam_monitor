import os
import threading
import sqlite3
from flask import Flask, jsonify
from dotenv import load_dotenv
from steam.client import SteamClient
from steam.enums import EPersonaState, EFriendRelationship

# 1. 加载环境变量
load_dotenv()

STEAM_USER = os.getenv("STEAM_USERNAME")
STEAM_PASS = os.getenv("STEAM_PASSWORD")


app = Flask(__name__)
client = SteamClient()

# 内存缓存与数据库名
friends_cache = {} 
DB_NAME = "steam_status.db"

# ==========================================
# 数据库逻辑
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
        print(f"❌ DB Error: {e}")

# ==========================================
# Web API 逻辑
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

def run_api_server():
    print("🌐 API Server running at http://localhost:5000/api/friends")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# ==========================================
# 解析逻辑 (修复 AttributeError)
# ==========================================
def parse_user_status(user):
    if not user: return None
    try:
        state_name = EPersonaState(user.state).name
        # 兼容性写法：使用 getattr 获取动态属性
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
        print(f"⚠️ Parse error for {user.steam_id}: {e}")
        return None

# ==========================================
# Steam 事件监听
# ==========================================
@client.on('logged_on')
def handle_logged_on():
    print("\n--- ✅ Logged in to Steam ---")
    if not client.friends.ready:
        client.friends.wait_event('ready')
    client.persona_state = EPersonaState.Online
    
    print(f"👥 Processing {len(client.friends)} contacts...")
    for friend in client.friends:
        if friend.relationship == EFriendRelationship.RequestRecipient:
            print(f"💌 Auto-accepting invite from: {friend.name or friend.steam_id}")
            client.friends.add(friend.steam_id)
            continue
            
        status = parse_user_status(friend)
        if status:
            friends_cache[status['steam_id']] = status
            
    threading.Thread(target=run_api_server, daemon=True).start()
    print("👀 Monitoring started...\n")

@client.friends.on('friend_invite')
def handle_friend_invite(user):
    print(f"💌 Real-time invite from {user.name or user.steam_id}, accepting...")
    client.friends.add(user.steam_id)

@client.on('persona_state_updated')
def handle_state_change(user, _):
    # 仅处理正式好友
    if user.relationship != EFriendRelationship.Friend:
        return

    new_s = parse_user_status(user)
    if not new_s: return
    
    sid = new_s['steam_id']
    old_s = friends_cache.get(sid, {})
    
    # 防抖校验
    if (new_s['state'] != old_s.get('state') or 
        new_s['game_appid'] != old_s.get('game_appid') or 
        new_s['rich_display'] != old_s.get('rich_display') or 
        new_s['party_id'] != old_s.get('party_id')):
        
        friends_cache[sid] = new_s
        log_to_db(new_s)
        
        g_info = f" | 🎮 {new_s['game_name']} ({new_s['rich_display']})" if new_s['game_name'] else ""
        print(f"[Update] {new_s['name']} -> {new_s['state']}{g_info}")

# 定义存放登录令牌的文件
LOGIN_KEY_FILE = "login_key.txt"

def my_login(username, password):
    """带自动令牌保存和读取的登录逻辑"""
    
    # 1. 尝试从本地文件读取旧的 login_key
    cached_login_key = None
    if os.path.exists(LOGIN_KEY_FILE):
        with open(LOGIN_KEY_FILE, "r") as f:
            cached_login_key = f.read().strip()
            print(f"🔑 发现本地登录令牌，尝试免码登录...")

    # 2. 执行登录
    # 如果有 cached_login_key，cli_login 会尝试直接登录而不弹验证码
    result = client.cli_login(
        username=username, 
        password=password, 
        login_key=cached_login_key
    )

    if result == 1: # EResult.OK
        # 3. 登录成功后，获取最新的 login_key 并保存
        # 即使这次是输 code 登录的，拿到这个 key 后下次就不用输了
        new_login_key = client.login_key
        if new_login_key:
            with open(LOGIN_KEY_FILE, "w") as f:
                f.write(new_login_key)
            print("💾 新的登录令牌已加密保存到本地。")
        return True
    else:
        print(f"❌ 登录失败，错误码: {result}")
        # 如果是因为令牌失效导致失败，可以考虑删掉文件重试
        if os.path.exists(LOGIN_KEY_FILE):
            os.remove(LOGIN_KEY_FILE)
        return False

# ==========================================
# 启动入口 (修改版)
# ==========================================
if __name__ == '__main__':
    if not STEAM_USER or not STEAM_PASS:
        print("❌ 错误: 请在 .env 文件中配置账号密码")
    else:
        init_db()
        print(f"🚀 正在为 {STEAM_USER} 启动监控程序...")
        
        # 使用我们自定义的登录函数
        if my_login(STEAM_USER, STEAM_PASS):
            # 只有登录成功后才运行
            client.run_forever()