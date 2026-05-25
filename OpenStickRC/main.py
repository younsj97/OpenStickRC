import os
import time
import pygame
import pigpio

# 모니터 없는 환경에서 pygame 비디오 에러 방지
os.environ["SDL_VIDEODRIVER"] = "dummy"

# --- 설정 값 ---
PPM_OUT_PIN = 17      # 조종기로 나가는 PPM 출력 핀
PPM_IN_PIN = 18       # 헤드트래커에서 들어오는 PPM 입력 핀

CHANNELS = 8          # 조종기로 보낼 총 PPM 채널 수
FRAME_MS = 22.5       # PPM 프레임 길이 (ms)
PULSE_LOW = 300       # 채널 간 펄스 폭 (us)

# 출력 범위 설정 (EdgeTX/OpenTX 기본 규격)
RC_MIN = 988
RC_CENTER = 1500
RC_MAX = 2011

# 헤드트래커 채널 설정 (기기마다 다를 수 있으므로 필요시 수정)
HT_TILT_INDEX = 4     # 헤드트래커 신호의 1번 채널을 Tilt로 간주
HT_PAN_INDEX = 5      # 헤드트래커 신호의 2번 채널을 Pan으로 간주
HT_TIMEOUT = 0.5      # 헤드트래커 신호가 이 시간(초) 동안 안 들어오면 연결 해제로 간주

class PPMReader:
    """pigpio 인터럽트를 사용해 외부 PPM 신호를 읽어들이는 클래스"""
    def __init__(self, pi, gpio):
        self.pi = pi
        self.gpio = gpio
        self.pi.set_mode(self.gpio, pigpio.INPUT)
        
        self.last_tick = None
        self.channels = [RC_CENTER] * 8
        self.current_chan = 0
        self.last_frame_time = 0
        
        # 핀의 상태가 상승(Rising Edge)할 때마다 _cbf 함수 호출
        self.cb = self.pi.callback(self.gpio, pigpio.RISING_EDGE, self._cbf)
        
    def _cbf(self, gpio, level, tick):
        if self.last_tick is not None:
            # 이전 펄스와 현재 펄스 사이의 마이크로초(us) 시간차 계산
            diff = pigpio.tickDiff(self.last_tick, tick)
            
            # 3000us 이상이면 프레임을 구분하는 Sync 펄스로 간주
            if diff > 3000: 
                self.current_chan = 0
                self.last_frame_time = time.time()
            # 800 ~ 2200us 사이면 유효한 RC 채널 데이터로 간주
            elif 800 <= diff <= 2200: 
                if self.current_chan < len(self.channels):
                    # 필터 없이 원본 데이터 그대로 저장
                    self.channels[self.current_chan] = diff
                    self.current_chan += 1
                    
        self.last_tick = tick

    def get_channels(self):
        """현재까지 읽어들인 채널 배열과, 신호 활성화 여부(True/False) 반환"""
        is_active = (time.time() - self.last_frame_time) < HT_TIMEOUT
        return self.channels, is_active

    def stop(self):
        self.cb.cancel()

class PPMGenerator:
    """pigpio의 DMA Wave를 사용하여 정밀한 PPM 신호를 생성하는 클래스"""
    def __init__(self, pi, gpio, channels, frame_ms):
        self.pi = pi
        self.gpio = gpio
        self.channels = channels
        self.frame_us = int(frame_ms * 1000)
        
        self.pi.set_mode(self.gpio, pigpio.OUTPUT)
        self.pi.write(self.gpio, 0)
        
        self.wave_id = None
        # 신호 잘림 방지를 위한 이전 파형 버퍼 변수
        self.stale_wave_id = None 

    def update(self, channel_values):
        pulses = []
        used_us = 0
        
        for val in channel_values:
            pulses.append(pigpio.pulse(0, 1 << self.gpio, PULSE_LOW))
            high_time = val - PULSE_LOW
            pulses.append(pigpio.pulse(1 << self.gpio, 0, high_time))
            used_us += val
            
        pulses.append(pigpio.pulse(0, 1 << self.gpio, PULSE_LOW))
        sync_time = self.frame_us - used_us - PULSE_LOW
        pulses.append(pigpio.pulse(1 << self.gpio, 0, sync_time))
        
        self.pi.wave_add_generic(pulses)
        new_wave = self.pi.wave_create()
        
        # REPEAT_SYNC 모드: 기존 파형 재생이 완전히 끝난 후 새 파형으로 넘어감
        self.pi.wave_send_using_mode(new_wave, pigpio.WAVE_MODE_REPEAT_SYNC)
        
        # 현재 재생 중일지도 모르는 직전 파형은 살려두고, 확실히 안 쓰는 두 세대 전 파형만 삭제
        if self.stale_wave_id is not None:
            self.pi.wave_delete(self.stale_wave_id)
            
        self.stale_wave_id = self.wave_id
        self.wave_id = new_wave

    def stop(self):
        self.pi.wave_tx_stop()
        if self.wave_id is not None:
            self.pi.wave_delete(self.wave_id)
        if self.stale_wave_id is not None:
            self.pi.wave_delete(self.stale_wave_id)

def safe_axis(joy, idx):
    return joy.get_axis(idx) if joy.get_numaxes() > idx else 0.0

def safe_btn(joy, idx):
    return joy.get_button(idx) if joy.get_numbuttons() > idx else 0

def axis_to_rc(axis_val):
    if axis_val >= 0:
        return int(RC_CENTER + axis_val * (RC_MAX - RC_CENTER))
    else:
        return int(RC_CENTER + axis_val * (RC_CENTER - RC_MIN))

def get_toggle_state(btn_up, btn_down):
    if btn_up: return RC_MIN
    elif btn_down: return RC_MAX
    return RC_CENTER

def main():
    pi = pigpio.pi()
    if not pi.connected:
        print("pigpio 데몬에 연결할 수 없습니다. 'sudo pigpiod'를 실행했는지 확인하세요.")
        return

    pi.wave_clear()

    pygame.init()
    pygame.joystick.init()

    # 입력(헤드트래커) 및 출력(조종기) 모듈 초기화
    ppm_out = PPMGenerator(pi, PPM_OUT_PIN, CHANNELS, FRAME_MS)
    ppm_in = PPMReader(pi, PPM_IN_PIN)
    
    devices = {"joy": None, "thr": None}

    print("--- FPV 컨트롤 시스템 (조이스틱 + 헤드트래커 통합) 시작 ---")
    print("입력 대기 중... (종료: Ctrl+C)")

    try:
        while True:
            for event in pygame.event.get():
                if event.type == pygame.JOYDEVICEADDED:
                    joy = pygame.joystick.Joystick(event.device_index)
                    name = joy.get_name().upper()
                    print(f"\n[USB 연결됨] {name}")
                    
                    if "T.16000" in name or "T16000" in name:
                        devices["joy"] = joy
                    elif "TWCS" in name or "THROTTLE" in name:
                        devices["thr"] = joy
                        
                elif event.type == pygame.JOYDEVICEREMOVED:
                    if devices["joy"] and event.instance_id == devices["joy"].get_instance_id():
                        print("\n[USB 해제됨] T.16000M 조이스틱")
                        devices["joy"].quit()
                        devices["joy"] = None
                    elif devices["thr"] and event.instance_id == devices["thr"].get_instance_id():
                        print("\n[USB 해제됨] TWCS 스로틀")
                        devices["thr"].quit()
                        devices["thr"] = None

            joy = devices["joy"]
            thr = devices["thr"]

            # 1. 기본값 세팅 (조이스틱/스로틀 연결 해제 시 기본값)
            ch_ail = RC_CENTER
            ch_ele = RC_CENTER
            ch_thr = RC_MIN
            ch_rud = RC_CENTER
            ch_gear = RC_CENTER

            # 2. 조이스틱 및 스로틀 데이터 덮어쓰기
            if joy:
                ch_ail = axis_to_rc(safe_axis(joy, 0))
                ch_ele = axis_to_rc(safe_axis(joy, 1))
                ch_rud = axis_to_rc(safe_axis(joy, 2))
                ch_thr = axis_to_rc(safe_axis(joy, 3))
                ch_gear = get_toggle_state(safe_btn(joy, 6), safe_btn(joy, 7))

            if thr:
                ch_thr = axis_to_rc(safe_axis(thr, 2))
                ch_rud = axis_to_rc(safe_axis(thr, 5))
                ch_gear = get_toggle_state(safe_btn(thr, 3), safe_btn(thr, 4))

            # 3. 헤드트래커 신호 처리
            ht_channels, ht_active = ppm_in.get_channels()
            if ht_active:
                ch_tilt = ht_channels[HT_TILT_INDEX]
                ch_pan = ht_channels[HT_PAN_INDEX]
                ht_status = "ON "
            else:
                # 분리되거나 신호가 없으면 중립(1500)
                ch_tilt = RC_CENTER
                ch_pan = RC_CENTER
                ht_status = "OFF"

            # 4. 채널 병합 및 출력
            rc_channels = [ch_ail, ch_ele, ch_thr, ch_rud, ch_tilt, ch_pan, ch_gear, RC_CENTER] # 순서대로 TR1~TR8 (8번 채널은 예비)
            
            # 신호 이탈 방지를 위해 전체 범위를 988~2011로 클리핑
            rc_channels = [max(RC_MIN, min(RC_MAX, val)) for val in rc_channels]

            ppm_out.update(rc_channels)

            # CLI에 데이터 출력 (디버깅용)
            print(f"\rAIL:{rc_channels[0]:4d} ELE:{rc_channels[1]:4d} THR:{rc_channels[2]:4d} "
                  f"RUD:{rc_channels[3]:4d} GEAR:{rc_channels[6]:4d} | "
                  f"HT[{ht_status}] TILT:{rc_channels[4]:4d} PAN:{rc_channels[5]:4d}   ", end="")

            time.sleep(0.005)

    except KeyboardInterrupt:
        print("\n프로그램을 종료합니다.")
    finally:
        ppm_in.stop()
        ppm_out.stop()
        pygame.quit()
        pi.stop()

if __name__ == "__main__":
    main()