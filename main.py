import network
import socket
import time
import random
import machine
from machine import Pin, I2C
import uasyncio as asyncio
from wifi_config import WIFI_SSID, WIFI_PASSWORD

# ===================== 설정값 =====================
LED_PIN = 16
NUM_LEDS = 10
BRIGHTNESS_MAX = 60  # 최대 밝기 (0~255 중 낮게 제한)

LEFT_LED_INDEX = 0      # 1번 LED (왼쪽 끝) - 인덱스 0
RIGHT_LED_INDEX = 9     # 10번 LED (오른쪽 끝) - 인덱스 9

# 허스키렌즈 I2C1 핀 (피코 기본 I2C1: SDA=6, SCL=7)
I2C1_SDA_PIN = 6
I2C1_SCL_PIN = 7

# WS2813 타이밍 값 (필수 지정)
WS2813_TIMING = (280, 515, 515, 745)

# 허스키렌즈 화면 해상도 & 좌우 판정 설정
HUSKYLENS_FRAME_WIDTH = 320  # 허스키렌즈 화면 가로 해상도 (기본 320)
DEAD_ZONE = 30               # 중앙 기준 +-30 픽셀은 애매한 것으로 판정
INVERT_LEFT_RIGHT = False    # 좌우가 반대로 인식되면 True로 바꿔보세요

# ===================== 전역 상태 =====================
game_state = {
    "status": "idle",       # idle, countdown, wait_result, result
    "countdown": 0,
    "led_side": None,       # "left" or "right"
    "result": None,         # "win", "lose", "draw", None
    "message": "게임을 시작하세요!",
    "face_x": None,
}

HUSKYLENS_OK = False

# ===================== WS2812/WS2813 LED 바 =====================
class NeoPixel:
    def __init__(self, pin, n, timing):
        self.pin = pin
        self.n = n
        self.timing = timing
        self.buf = bytearray(n * 3)
        self._np = machine.Pin(pin, machine.Pin.OUT)

    def __setitem__(self, i, color):
        r, g, b = color
        self.buf[i * 3] = g
        self.buf[i * 3 + 1] = r
        self.buf[i * 3 + 2] = b

    def fill(self, color):
        for i in range(self.n):
            self[i] = color

    def write(self):
        machine.bitstream(
            self._np,
            0,
            self.timing,
            self.buf
        )

led_bar = NeoPixel(LED_PIN, NUM_LEDS, WS2813_TIMING)

def scale_brightness(color, brightness=BRIGHTNESS_MAX):
    r, g, b = color
    factor = brightness / 255
    return (int(r * factor), int(g * factor), int(b * factor))

def clear_leds():
    led_bar.fill((0, 0, 0))
    led_bar.write()

def show_led(index, color):
    clear_leds()
    led_bar[index] = scale_brightness(color)
    led_bar.write()

def show_countdown_color(count):
    colors = {3: (255, 0, 0), 2: (255, 255, 0), 1: (0, 255, 0)}
    color = colors.get(count, (0, 0, 0))
    clear_leds()
    mid = NUM_LEDS // 2
    led_bar[mid - 1] = scale_brightness(color)
    led_bar[mid] = scale_brightness(color)
    led_bar.write()

# ===================== 허스키렌즈 (I2C, Face Recognition) =====================
class HuskyLensI2C:
    ADDRESS = 0x32

    CMD_REQUEST = 0x20
    CMD_REQUEST_BLOCKS = 0x21

    def __init__(self, i2c):
        self.i2c = i2c

    def _checksum(self, data):
        return sum(data) & 0xFF

    def _build_request(self, cmd, content=b''):
        packet = bytearray()
        packet.append(0x55)
        packet.append(0xAA)
        packet.append(0x11)
        packet.append(len(content))
        packet.append(cmd)
        packet.extend(content)
        checksum = self._checksum(packet)
        packet.append(checksum)
        return packet

    def request_blocks(self):
        """얼굴 인식 결과(블록) 요청 - 가장 최근 프레임 정보 요청"""
        try:
            packet = self._build_request(self.CMD_REQUEST)
            self.i2c.writeto(self.ADDRESS, packet)
            time.sleep_ms(10)
            data = self.i2c.readfrom(self.ADDRESS, 50)
            return self._parse_blocks(data)
        except Exception as e:
            print("HuskyLens read error:", e)
            return []

    def _parse_blocks(self, data):
        """
        허스키렌즈 I2C 프로토콜 파싱 (단순화된 버전)
        블록 데이터 형식: xCenter(2) yCenter(2) width(2) height(2) ID(2)
        """
        blocks = []
        i = 0
        while i < len(data) - 1:
            if data[i] == 0x55 and i + 1 < len(data) and data[i+1] == 0xAA:
                try:
                    cmd = data[i+4]
                    content_len = data[i+3]
                    if cmd in (0x2A, 0x2B):  # 블록 반환 명령
                        content_start = i + 5
                        content = data[content_start:content_start + content_len]
                        if len(content) >= 10:
                            x_center = content[0] | (content[1] << 8)
                            y_center = content[2] | (content[3] << 8)
                            width = content[4] | (content[5] << 8)
                            height = content[6] | (content[7] << 8)
                            block_id = content[8] | (content[9] << 8)
                            blocks.append({
                                "x": x_center,
                                "y": y_center,
                                "w": width,
                                "h": height,
                                "id": block_id
                            })
                        i = content_start + content_len + 1
                        continue
                except IndexError:
                    pass
            i += 1
        return blocks


def init_husky():
    global HUSKYLENS_OK, husky
    try:
        i2c = I2C(1, sda=Pin(I2C1_SDA_PIN), scl=Pin(I2C1_SCL_PIN), freq=100000)
        husky = HuskyLensI2C(i2c)
        devices = i2c.scan()
        print("I2C 장치 스캔:", devices)
        if HuskyLensI2C.ADDRESS in devices:
            HUSKYLENS_OK = True
            print("허스키렌즈 연결 확인됨")
        else:
            print("허스키렌즈를 찾을 수 없습니다. (더미 모드로 동작)")
    except Exception as e:
        print("허스키렌즈 초기화 실패:", e)
        HUSKYLENS_OK = False


def get_face_x():
    """가장 최근 얼굴의 X 중심 좌표를 반환. 인식 실패 시 None"""
    if not HUSKYLENS_OK:
        # 허스키렌즈가 없을 때 테스트용 랜덤값
        return random.choice([80, 240])
    blocks = husky.request_blocks()
    if blocks:
        x = blocks[0]["x"]
        print("[디버그] 얼굴 X좌표:", x)
        return x
    return None


def judge_face_side(face_x):
    """중앙 데드존을 고려하여 좌/우 판정. 애매하면 None 반환"""
    center = HUSKYLENS_FRAME_WIDTH // 2

    if face_x < center - DEAD_ZONE:
        side = "left"
    elif face_x > center + DEAD_ZONE:
        side = "right"
    else:
        return None  # 애매한 중앙 위치

    if INVERT_LEFT_RIGHT:
        side = "right" if side == "left" else "left"

    return side


# ===================== 게임 로직 =====================
async def run_game():
    game_state["status"] = "countdown"
    game_state["result"] = None
    game_state["face_x"] = None

    # LED 방향 랜덤 결정
    led_side = random.choice(["left", "right"])
    game_state["led_side"] = led_side

    # 카운트다운 3-2-1
    for count in [3, 2, 1]:
        game_state["countdown"] = count
        game_state["message"] = "{}...".format(count)
        show_countdown_color(count)
        await asyncio.sleep(1)

    # LED 점등
    led_index = LEFT_LED_INDEX if led_side == "left" else RIGHT_LED_INDEX
    led_color = (0, 0, 255) if led_side == "left" else (255, 0, 100)
    show_led(led_index, led_color)

    game_state["status"] = "wait_result"
    game_state["message"] = "{} LED 점등! 얼굴을 인식합니다...".format(
        "왼쪽" if led_side == "left" else "오른쪽"
    )

    await asyncio.sleep(0.3)  # LED 점등 후 잠깐 대기 (반응 시간)

    # 얼굴 인식 (데드존을 벗어난 확실한 방향이 나올 때까지 최대 2초 시도)
    face_x = None
    face_side = None
    for _ in range(20):
        candidate_x = get_face_x()
        if candidate_x is not None:
            face_x = candidate_x
            face_side = judge_face_side(candidate_x)
            if face_side is not None:
                break
        await asyncio.sleep(0.1)

    clear_leds()

    if face_x is None or face_side is None:
        game_state["status"] = "result"
        game_state["result"] = "draw"
        game_state["message"] = "얼굴 방향을 명확히 인식하지 못했습니다. 다시 시도하세요!"
        game_state["face_x"] = face_x
        return

    game_state["face_x"] = face_x

    if face_side == led_side:
        result = "lose"
        message = "같은 방향! 플레이어 패배 ㅠㅠ (LED: {}, 고개: {})".format(led_side, face_side)
    else:
        result = "win"
        message = "다른 방향! 플레이어 승리! (LED: {}, 고개: {})".format(led_side, face_side)

    game_state["status"] = "result"
    game_state["result"] = result
    game_state["message"] = message


# ===================== 와이파이 연결 =====================
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    print("와이파이 연결 중...")
    timeout = 20
    while timeout > 0:
        if wlan.isconnected():
            break
        timeout -= 1
        time.sleep(1)
        print(".", end="")

    if wlan.isconnected():
        print("\n와이파이 연결 성공!")
        print("IP 주소:", wlan.ifconfig()[0])
        return wlan.ifconfig()[0]
    else:
        print("\n와이파이 연결 실패!")
        return None


# ===================== 웹 대시보드 HTML =====================
def get_html_page():
    return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>참참참 게임</title>
<style>
  body {
    font-family: 'Malgun Gothic', sans-serif;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    text-align: center;
    margin: 0;
    padding: 20px;
    min-height: 100vh;
  }
  h1 { font-size: 28px; margin-bottom: 5px; }
  .card {
    background: rgba(255,255,255,0.15);
    border-radius: 20px;
    padding: 25px;
    margin: 20px auto;
    max-width: 400px;
    box-shadow: 0 8px 20px rgba(0,0,0,0.3);
  }
  .countdown {
    font-size: 80px;
    font-weight: bold;
    margin: 20px 0;
  }
  .message {
    font-size: 20px;
    margin: 15px 0;
    min-height: 60px;
  }
  .result-win { color: #4ade80; font-weight: bold; font-size: 26px; }
  .result-lose { color: #f87171; font-weight: bold; font-size: 26px; }
  .result-draw { color: #fbbf24; font-weight: bold; font-size: 26px; }
  button {
    background: #ff6b6b;
    color: white;
    border: none;
    padding: 15px 40px;
    font-size: 20px;
    border-radius: 50px;
    cursor: pointer;
    margin-top: 15px;
    box-shadow: 0 4px 10px rgba(0,0,0,0.3);
  }
  button:active { transform: scale(0.95); }
  .led-indicator {
    font-size: 18px;
    margin-top: 10px;
    padding: 10px;
    border-radius: 10px;
    background: rgba(0,0,0,0.2);
  }
</style>
</head>
<body>
  <h1>🎮 참참참 게임</h1>
  <p>얼굴을 왼쪽/오른쪽으로 돌려서 LED와 반대 방향이면 승리!</p>

  <div class="card">
    <div class="countdown" id="countdown">-</div>
    <div class="message" id="message">게임을 시작하세요!</div>
    <div class="led-indicator" id="ledInfo"></div>
    <button onclick="startGame()">게임 시작</button>
  </div>

<script>
function startGame() {
  fetch('/start').then(r => r.json()).then(data => {
    console.log(data);
  });
}

function updateStatus() {
  fetch('/status').then(r => r.json()).then(data => {
    document.getElementById('message').innerText = data.message;

    if (data.status === 'countdown') {
      document.getElementById('countdown').innerText = data.countdown;
      document.getElementById('ledInfo').innerText = '';
    } else if (data.status === 'wait_result') {
      document.getElementById('countdown').innerText = '👀';
      document.getElementById('ledInfo').innerText =
        (data.led_side === 'left' ? '⬅️ 왼쪽' : '➡️ 오른쪽') + ' LED 점등!';
    } else if (data.status === 'result') {
      document.getElementById('countdown').innerText =
        data.result === 'win' ? '🎉' : (data.result === 'lose' ? '😢' : '🤔');
      let msgEl = document.getElementById('message');
      msgEl.className = 'message result-' + data.result;
      document.getElementById('ledInfo').innerText =
        'LED 방향: ' + (data.led_side === 'left' ? '왼쪽' : '오른쪽') +
        ' | 얼굴 X좌표: ' + (data.face_x !== null ? data.face_x : '인식 안됨');
    } else {
      document.getElementById('countdown').innerText = '-';
      document.getElementById('ledInfo').innerText = '';
      document.getElementById('message').className = 'message';
    }
  });
}

setInterval(updateStatus, 500);
updateStatus();
</script>
</body>
</html>
"""


# ===================== 웹 서버 =====================
async def handle_client(reader, writer):
    try:
        request_line = await reader.readline()
        request = request_line.decode()

        while True:
            line = await reader.readline()
            if line == b'\r\n' or line == b'':
                break

        if "GET /start" in request:
            asyncio.create_task(run_game())
            response_body = '{"status": "started"}'
            response = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + response_body

        elif "GET /status" in request:
            import ujson
            response_body = ujson.dumps({
                "status": game_state["status"],
                "countdown": game_state["countdown"],
                "led_side": game_state["led_side"],
                "result": game_state["result"],
                "message": game_state["message"],
                "face_x": game_state["face_x"],
            })
            response = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + response_body

        else:
            html = get_html_page()
            response = "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + html

        await writer.awrite(response)
    except Exception as e:
        print("클라이언트 처리 에러:", e)
    finally:
        await writer.aclose()


async def main():
    clear_leds()
    init_husky()

    ip = connect_wifi()
    if ip is None:
        print("=" * 40)
        print("와이파이 연결 실패!")
        print("1. wifi_config.py의 SSID/비밀번호 확인")
        print("2. 공유기가 2.4GHz를 지원하는지 확인")
        print("=" * 40)
        return

    print("=" * 40)
    print("웹 대시보드 주소: http://{}".format(ip))
    print("스마트폰 브라우저에 위 주소를 정확히 입력하세요!")
    print("=" * 40)

    # 혹시 이전 실행에서 열려있던 소켓이 있다면 강제로 정리
    try:
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        temp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        temp_sock.bind(("0.0.0.0", 80))
        temp_sock.close()
        print("포트 80 정리 완료")
    except OSError as e:
        print("포트 정리 중 참고:", e)

    try:
        server = await asyncio.start_server(handle_client, "0.0.0.0", 80)
        print("웹 서버 시작됨 (포트 80) - 정상 작동 중")
    except Exception as e:
        print("웹 서버 시작 실패:", e)
        return

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        clear_leds()
        print("프로그램 종료")

