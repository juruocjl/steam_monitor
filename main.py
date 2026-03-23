import os
import sqlite3
import threading
import asyncio
from flask import Flask, jsonify
from dotenv import load_dotenv
import steam  # 注意：这必须是通过 uv add steamio 安装的

# 1. 加载配置
load_dotenv()
STEAM_USER = os.getenv("STEAM_USERNAME")
STEAM_PASS = os.getenv("STEAM_PASSWORD")

# 2. 全局数据存储
friends_cache = {}
DB_NAME = "steam_status.db"

# ==========================================
# Flask Web 服务 (独立运行)
# ==========================================
app = Flask(__name__)

@app.route('/api/friends', methods=['GET'])
def get_friends():
    return jsonify({"status": "success", "data": list(friends_cache.values())})

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# ==========================================
# 核心逻辑：继承 steam.Client (官方 Example 范式)
# ==========================================
class SteamMonitor(steam.Client):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.init_db()

    def init_db(self):
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
                        party_id TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )''')
        conn.commit()
        conn.close()

    def log_to_db(self, data):
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute('''INSERT INTO status_log 
                         (steam_id, name, state, game_appid, game_name, rich_display, party_id) 
                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                      (data['steam_id'], data['name'], data['state'], 
                       data['game_appid'], data['game_name'], 
                       data['rich_display'], data['party_id']))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ DB Error: {e}")

    def parse_user_to_dict(self, user):
        """对照 steamio 文档解析 User 对象"""
        game = user.game
        # steamio 中 Game 对象有 rich_presence 属性
        rp = game.rich_presence if game else {}
        
        return {
            "steam_id": str(user.id64),
            "name": user.name,
            "state": str(user.status), # steamio 使用 .status
            "game_appid": str(game.id) if game else "",
            "game_name": game.name if game else "",
            "rich_display": rp.get('steam_display', ''),
            "party_id": rp.get('steam_player_group', '')
        }

    # --- 官方事件重写 ---
    
    async def on_ready(self):
        """当机器人成功连接并准备好后调用"""
        print(f"\n--- ✅ 登录成功！账号: {self.user.name} ---")
        
        # 初始化缓存
        for friend in self.friends:
            data = self.parse_user_to_dict(friend)
            friends_cache[data['steam_id']] = data
        
        print(f"👥 已监视 {len(self.friends)} 名好友。")
        
        # 启动 Flask 线程
        threading.Thread(target=run_flask, daemon=True).start()
        print("🌐 API 已就绪: http://localhost:5000/api/friends\n")

    async def on_user_update(self, before, after):
        """核心：当好友信息变动时触发"""
        # 只看好友，不看陌生人
        if after.relationship != steam.Relationship.Friend:
            return

        new_data = self.parse_user_to_dict(after)
        old_data = friends_cache.get(new_data['steam_id'], {})

        # 变动检测：状态改变、游戏改变或富状态改变
        if (new_data['state'] != old_data.get('state') or 
            new_data['game_appid'] != old_data.get('game_appid') or 
            new_data['rich_display'] != old_data.get('rich_display')):
            
            friends_cache[new_data['steam_id']] = new_data
            self.log_to_db(new_data)
            
            # 控制台输出日志
            game_msg = f" | 🎮 {new_data['game_name']}" if new_data['game_name'] else ""
            rp_msg = f" ({new_data['rich_display']})" if new_data['rich_display'] else ""
            print(f"✨ [变动] {new_data['name']} -> {new_data['state']}{game_msg}{rp_msg}")

# ==========================================
# 启动
# ==========================================
if __name__ == "__main__":
    # 根据 steamio 官方 Example 的启动方式
    client = SteamMonitor()
    try:
        # 这个 run 会阻塞当前线程，并自动处理 asyncio 循环
        client.run(STEAM_USER, STEAM_PASS)
    except KeyboardInterrupt:
        print("\n🛑 程序已手动停止。")