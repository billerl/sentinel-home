// Sentinel Wi-Fi motion beacon (any ESP32-S3, e.g. a Seeed XIAO).
//
// The motion sensor (firmware/wifi_motion) measures the radio channel on frames it receives. Pinging
// the home router works only while the router answers with 802.11g/n frames: many routers drop a
// weaker link to 802.11b, which carries no channel information, and readings stop. This board fixes
// that: it broadcasts a small ESP-NOW frame 50 times a second at a fixed 802.11n rate (HT20 MCS0)
// on the home Wi-Fi's channel. The sensor hears it and measures the path between the two boards.
//
// It never joins the Wi-Fi (no password needed): it looks up which channel the home network uses
// (HOME_SSID in secrets.h, git-ignored) and checks again every few minutes in case the router moved.
// Power it from any USB charger; updates go over USB.

#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_now.h>
#include "secrets.h"  // #define HOME_SSID "..." (copy secrets.example.h)

#define FW_VERSION "1.0.0"
#define SEND_INTERVAL_MS 20
#define RESCAN_MS (5 * 60 * 1000)

static const uint8_t BROADCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
static uint8_t channel = 0;
static int routerRssi = 0;  // how well the home Wi-Fi is heard: very weak right next to it = no antenna fitted
static uint32_t sent = 0, failed = 0;

struct __attribute__((packed)) BeaconFrame {
  char magic[6];  // "SNTLB1": the sensor only listens to frames that start with this
  uint32_t seq;
};

static uint8_t findChannel() {
  int n = WiFi.scanNetworks(false, true);
  int best = -1;
  for (int i = 0; i < n; i++) {
    if (WiFi.SSID(i) == HOME_SSID && (best < 0 || WiFi.RSSI(i) > WiFi.RSSI(best))) best = i;
  }
  uint8_t ch = best >= 0 ? WiFi.channel(best) : 0;
  if (best >= 0) routerRssi = WiFi.RSSI(best);
  WiFi.scanDelete();
  return ch;
}

static void useChannel(uint8_t ch) {
  esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
  channel = ch;
  Serial.printf("beacon: sending on channel %u\n", ch);
}

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_ps(WIFI_PS_NONE);
  uint8_t ch = 0;
  while (!(ch = findChannel())) {
    Serial.printf("beacon: can't see '%s' yet, looking again\n", HOME_SSID);
    delay(5000);
  }
  useChannel(ch);
  esp_now_init();
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST, 6);
  peer.channel = 0;  // = whatever channel the radio is on
  peer.ifidx = WIFI_IF_STA;
  esp_now_add_peer(&peer);
  esp_now_rate_config_t rate = {};
  rate.phymode = WIFI_PHY_MODE_HT20;
  rate.rate = WIFI_PHY_RATE_MCS0_LGI;  // 802.11n: carries the channel information the sensor needs
  esp_now_set_peer_rate_config(BROADCAST, &rate);
}

void loop() {
  static uint32_t lastSend = 0, lastScan = millis(), lastNote = 0;
  uint32_t now = millis();
  if (now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;
    BeaconFrame f = {{'S', 'N', 'T', 'L', 'B', '1'}, sent};
    if (esp_now_send(BROADCAST, (const uint8_t *)&f, sizeof(f)) == ESP_OK) sent++; else failed++;
  }
  if (now - lastScan > RESCAN_MS) {  // the router may change channel (after a restart, or on its own)
    lastScan = now;
    uint8_t ch = findChannel();
    useChannel(ch ? ch : channel);  // scanning hops channels: always go back to one
  }
  if (now - lastNote > 10000) {
    lastNote = now;
    Serial.printf("beacon %s: channel %u, home Wi-Fi heard at %d dBm, %lu sent, %lu failed\n", FW_VERSION, channel,
                  routerRssi, (unsigned long)sent, (unsigned long)failed);
  }
  delay(1);
}
