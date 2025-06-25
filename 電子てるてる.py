from machine import Pin, PWM, I2C
import time
import neopixel
import urequests
import ujson
import network
import uasyncio as asyncio
import ntptime
import ssd1306

# 気象庁JSONのURL(大阪府 270000)
url = "https://www.jma.go.jp/bosai/forecast/data/forecast/270000.json"

# ===== 天気情報取得時刻宣言 =====
# 2000年1月1日 7:30:00 の struct_time を定義
acquire_time_base = (2000, 1, 1, 7, 30, 0, 0, 0)
# エポック秒に変換（UTC基準）
epoch_sec = time.mktime(acquire_time_base)
# エポック秒から struct_time に戻す
acquire_time = time.localtime(epoch_sec)

# システム現在時刻格納変数
now_time=time.localtime()

# NTPサーバーから時刻を取得
def sync_time():
    global now_time
    try:
        ntptime.settime()  # UTCで設定される（日本時間にするには補正が必要）
        print("NTP時刻を取得しました。")
        get_japan_time()
    except Exception as e:
        print("NTP取得失敗:", e)

# 日本時間に補正（+9時間）
def get_japan_time():
    global now_time,acquire_time
    now_time = time.localtime(time.time() + 9*3600)
    
    # 明日の日付を計算
    year = now_time[0]
    month = now_time[1]
    day = now_time[2] + 1  #うるう年以外は対応可能
    # 明日 7:30:00 を struct_time として作成（weekday, yearday は仮）
    t_target = (year, month, day, 7, 30, 0, 0, 0)
    
    
    # mktimeでエポック秒に
    epoch = time.mktime(t_target)
    acquire_time = time.localtime(epoch)
    
    print("システム時刻:", now_time)
    print("取得実行時刻:", acquire_time)
    print(f"{now_time[0]}/{now_time[1]:02}/{now_time[2]:02} {now_time[3]:02}:{now_time[4]:02}:{now_time[5]:02}")
    print(f"{acquire_time[0]}/{acquire_time[1]:02}/{acquire_time[2]:02} {acquire_time[3]:02}:{acquire_time[4]:02}:{acquire_time[5]:02}")
    
# ===== OLED設定（GPIO12,13） =====
# I2C初期化
i2c = I2C(0, scl=Pin(13), sda=Pin(12))

# OLEDの初期化（128x32ピクセル用）
oled = ssd1306.SSD1306_I2C(128, 32, i2c)

    
# ===== サーボモーター設定（GPIO21） =====
servo = PWM(Pin(21))
servo.freq(50)

def set_servo_angle(angle):
    duty = int((angle / 180) * 6553 + 1638)  # 0度〜180度を16bitにマッピング
    servo.duty_u16(duty)


# ===== スイッチ設定（GPIO19） =====
switch = Pin(19, Pin.IN, Pin.PULL_UP)

# カウント変数
count = 0
switch_flag = False

# スイッチ監視タスク
async def monitor_switch():
    global count,switch_flag
    last_state = 1
    while True:
        current_state = switch.value()
        if current_state == 0 and last_state == 1:
            # スイッチが「押された瞬間」を検出（立下り）
            count += 1
            print("カウント:", count)
            switch_flag = True
        last_state = current_state
        await asyncio.sleep_ms(20)  # チャタリング防止
        
        
#  ===== Wi-Fi接続情報 =====
SSID = ''
PASSWORD = ''

path = '/wifi_pass.txt'  # wifi設定ファイルパスの指定
with open(path, 'r') as f:  
    for line in f:
        line = line.strip()  

        if line.startswith("SSID:"):
            SSID = line.split(":")[1].strip()
        elif line.startswith("PASSWORD:"):
            PASSWORD = line.split(":")[1].strip()
print('SSID:',SSID)
print('PASSWORD:',PASSWORD)
                        

# ===== NeoPixel設定（GPIO20、16個） =====
NUM_LEDS = 16
np = neopixel.NeoPixel(Pin(20), NUM_LEDS)
goal_np = [[0 for _ in range(16)] for _ in range(3)]

# 補間のステップ数（数が多いほど滑らか）
steps_per_transition = 50
delay_per_step = 0.005  # 秒

def fade():
    before_np = [[0 for _ in range(16)] for _ in range(3)]
    # 分解して格納
    for i in range(16):
        r, g, b = np[i]
        before_np[0][i] = r
        before_np[1][i] = g
        before_np[2][i] = b
    
    for i in range(steps_per_transition):
        for j in range(NUM_LEDS):
            np[j]=(int((before_np[0][j]+(goal_np[0][j]-before_np[0][j])*i/steps_per_transition)),int((before_np[1][j]+(goal_np[1][j]-before_np[1][j])*i/steps_per_transition)),int((before_np[2][j]+(goal_np[2][j]-before_np[2][j])*i/steps_per_transition)))
        np.write()
        time.sleep(delay_per_step)
        
    for i in range(NUM_LEDS):
        np[i]=(goal_np[0][i],goal_np[1][i],goal_np[2][i])
    np.write()
    
def error_movement():
    for i in range(NUM_LEDS):
        np[i] = (0, 0, 0)
    np.write()
    
    angle=0
    set_servo_angle(70)
    for i in range(10):
        set_servo_angle(70-i*7)
        time.sleep(0.1)
    

#  ===== weather_summary読み込み =====
# ファイルを開いて読み込む
with open("weather_summary.txt", "r", encoding="utf-8") as f:
    lines = f.readlines()

# 辞書に格納（キー: 天気コード, 値: 辞書またはタプル）
weather_data = {}

for line in lines:
    parts = line.strip().split(":")
    if len(parts) == 5:
        code = parts[0].strip()
        summary = parts[1].strip()
        r = int(parts[2].strip())
        g = int(parts[3].strip())
        b = int(parts[4].strip())
        
        weather_data[code] = {
            "summary": summary,
            "color": (r, g, b)
        }
        
# ===== wifi接続~天気情報取得 =====
def get_connectwifi_wheather_data(leave_flag=False): # leave_flag=Trueの場合、取得後も表示を続ける
        
    try:
        # ステーション（STA）モードでWi-Fi有効化
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)

        # 接続開始
        wlan.connect(SSID, PASSWORD)
        
        # 接続完了まで待つ（タイムアウト付き）
        max_wait = 10
        while max_wait > 0:
            if wlan.isconnected():
                break
            print('接続中...')
            time.sleep(1)
            max_wait -= 1
            
        # 接続結果確認
        if wlan.isconnected():
            print('Wi-Fi接続成功!')
            print('IPアドレス:', wlan.ifconfig()[0])

            get_weather_data(leave_flag)
            
        else:
            print('wifi接続失敗')
            error_movement()

    except Exception as e:
        print("その他の予期せぬエラー:", e)
        error_movement()
                
                
# ===== 天気情報取得関数 =====
def get_weather_data(leave_flag=False): # leave_flag=Trueの場合、取得後も表示を続ける
    for i in range(NUM_LEDS):
        for j in range(3):
            goal_np[j][i] = 0
    fade()

    # NTPサーバーから時刻を取得
    sync_time()
    
    # 天気情報JSONの取得
    # リクエストを送信
    response = urequests.get(url)

    # JSONを読み取り
    data = ujson.loads(response.text)
    response.close()
    
    for i in range(NUM_LEDS):
        for j in range(3):
            goal_np[j][i] = 50
    fade()
        
        
    for angle in [70, 150, 70, 150, 70, 150, 70]:
        print("サーボ角度:", angle)
        set_servo_angle(angle)
        time.sleep(0.2)
        
    for i in range(NUM_LEDS):
        for j in range(3):
            goal_np[j][i] = 0
    fade()

    # ---------- 天気コード抽出 ----------
    # weatherCodes の1つ目を抽出
    first_weather_code = data[0]['timeSeries'][0]['areas'][0]['weatherCodes'][0]

    # 結果を表示
    print("今日の天気コード:", first_weather_code)
     
    # LEDに天気に応じた色をセット
    print(weather_data[first_weather_code]["color"])
    r, g, b =  weather_data[first_weather_code]["color"]  
    for i in range(NUM_LEDS):
        goal_np[0][i] = r
        goal_np[1][i] = g
        goal_np[2][i] = b
    
    # ---------- 降水確率（pops）抽出 ----------
    # 降水確率（pops）のすべてを取得
    # 最初の timeSeries ブロックの 2番目（index=1）に含まれている
    oled.fill(0)  # 画面クリア（0=黒）
    pops = data[0]["timeSeries"][1]["areas"][0]["pops"]
    oled.text("POPS:", 0, 0)
    print("降水確率一覧:")
    for i, pop in enumerate(pops):
        print(f"{i}番目: {pop}%")
        if i<4:
            oled.text(f"{pop}%", i*32, 8)
        elif i<8:
            oled.text(f"{pop}%", (i-4)*32, 16)
            
        if i+12<16: # LED個数を超えないようマッピング
            goal_np[0][12+i] = int(2.5*(100-int(pop)))
            goal_np[1][12+i] = int(2.5*(100-int(pop)))
            goal_np[2][12+i] = 255

    fade()
    oled.show()
    
    get_japan_time()
    
    set_servo_angle(70)
    for i in range(100):
        set_servo_angle(70+i)
        time.sleep(0.05)
        
    set_servo_angle(170)
    
    if leave_flag==False:# leave_flag=Falseの場合、てるてると照明を元に戻す
        for i in range(100):
            set_servo_angle(170-i)
            time.sleep(0.05)
            
        set_servo_angle(70)
        
        for i in range(NUM_LEDS):
            for j in range(3):
                goal_np[j][i] = 0
        fade()
        
                    
# ===== メインループ =====

async def main_loop():
    global count,switch_flag,now_time,acquire_time
    
    oled.fill(0)  # 画面クリア（0=黒）
    oled.show()
    
    time_count=0
    while True:
        if switch_flag == True:
            switch_flag = False
            
            for i in range(NUM_LEDS):
                for j in range(3):
                    goal_np[j][i] = 50
            fade()
            
            for angle in [70, 150, 70, 150, 70]:
                print("サーボ角度:", angle)
                set_servo_angle(angle)
                time.sleep(0.2)
                
            get_connectwifi_wheather_data()
            
        await asyncio.sleep(0.001)  # 他の処理がある場合に備えた待機
        
        time_count = time_count + 1
        if time_count>20000: # 20秒おきに時刻チェック
            print("時刻チェック")
            time_count=0
            
            sync_time()
            print(f"{now_time[0]}/{now_time[1]}/{now_time[2]} {now_time[3]}:{now_time[4]}")
            print(f"{acquire_time[0]}/{acquire_time[1]}/{acquire_time[2]} {acquire_time[3]}:{acquire_time[4]}")
            
            oled.fill(0)  # 画面クリア（0=黒）
#             oled.fill_rect(0, 24, 128, 8, 0)
#             oled.text(f"{now_time[0]}/{now_time[1]}/{now_time[2]} {now_time[3]}:{now_time[4]}", 0, 24)
            oled.show()

            if (now_time[0],now_time[1],now_time[2],now_time[3],now_time[4]) == (acquire_time[0],acquire_time[1],acquire_time[2],acquire_time[3],acquire_time[4]):
                print("時刻一致")
                get_connectwifi_wheather_data(True)
                
            
            
# 非同期で2つのタスクを同時起動
async def main():
    asyncio.create_task(monitor_switch())  # スイッチ監視
    await main_loop()                      # メインループ

asyncio.run(main())