// Sentinel Wi-Fi motion sensor (any ESP32-S3: Adafruit Feather, Seeed XIAO, generic S3 boards).
//
// People moving between two boards disturb the radio signal between them. Every board broadcasts a
// small ESP-NOW frame 50 times a second at a fixed 802.11n rate (HT20 MCS0) and reads the channel state
// information (CSI) of the frames it hears from the others: the strength of 52 sub-carriers. A still
// room gives a steady pattern; movement makes it wobble. So every pair of boards is a "line" through
// the house, measured from both ends. With no other board in reach, the board pings the router ~50
// times a second and measures its replies instead. Every 250 ms the board turns the last second of
// wobble into one "score" per line. Sentinel polls GET /status and decides what counts as motion
// (thresholds live there, so they can be tuned without reflashing).
//
// First start (or BOOT button held 3 s): the board opens an open Wi-Fi network
// "Sentinel-Sensor-XXXX"; join it on a phone and pick the home Wi-Fi. A board on USB can be given it
// over serial instead: one line "wifi<TAB>ssid<TAB>password". The password is stored on the board only.
// Updates go over Wi-Fi (ArduinoOTA, password in secrets.h, which is git-ignored).

#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include <Wire.h>
#include <esp_wifi.h>
#include <esp_now.h>
#include <esp_mac.h>
#include "ping/ping_sock.h"
#include "lwip/ip_addr.h"
#include "secrets.h"  // #define OTA_PASSWORD "..." (copy secrets.example.h)

#define FW_VERSION "2.1.0"
#define HELLO_PORT 47301          // UDP broadcast so Sentinel can find the board
#define PING_INTERVAL_MS 20       // 50 pings/s: each reply carries one CSI reading
#define SEND_INTERVAL_MS 20       // 50 frames/s for the other boards to measure
#define WINDOW 50                 // readings per score (~1 s)
#define HISTORY 40                // scores kept for /status (10 s at 4/s)
#define NSUB 52                   // usable HT20 sub-carriers (1..26, 38..63)
#define BOOT_PIN 0
#define MAX17048_ADDR 0x36
// Stack sizes: the boards crashed every 1-3 min with "Stack canary watchpoint triggered" once 5-7 boards were
// talking (2026-11-09). /status reports how much of each stayed unused ("stack_free_*").
#define SEND_STACK 6144
#define LOOP_STACK (16 * 1024)

static Preferences prefs;
static WebServer server(80);
static DNSServer dns;
static WiFiUDP udp;
static String devId, hostName;
static bool setupMode = false;
static bool no11b = false;  // connected without 802.11b (see setup())

// ---- links: one per transmitter we measure (filled in the Wi-Fi task, read in loop) ----
// Link 0 is the router (its replies to our pings); the others are the other boards (this firmware, or the
// send-only firmware/wifi_motion_beacon), each broadcasting 50 fixed-rate 802.11n frames a second. Each link
// is a separate line through the house, so each gets its own score. The router link is used only while no
// other board is heard: routers fall back to 802.11b on a weak link, which carries no CSI.
#define MAX_LINKS 8  // the router + 7 other boards (was 6: the 7th board of 2026-11-09 left one line out)
#define BEACON_GONE_MS 3000      // a beacon not heard for this long is "not active"
#define BEACON_FORGET_MS 60000   // ...and after this long it's left out of /status

struct Sample { uint32_t seq; float score; float rate; };

struct Link {
  uint8_t mac[6];
  volatile bool used;
  volatile uint32_t seenMs;   // beacons: last frame with our marker
  float amp[WINDOW][NSUB];    // per-frame amplitudes, normalised (ring)
  volatile uint32_t count;    // readings ever
  volatile int8_t rssi;
  uint32_t lastCount;
  Sample history[HISTORY];
};

static portMUX_TYPE csiMux = portMUX_INITIALIZER_UNLOCKED;
static Link links[MAX_LINKS];
static uint32_t sampleSeq = 0, lastScoreMs = 0;
// Diagnostics for /status.
static volatile uint32_t csiAll = 0, csiShort = 0, csiOther = 0, beaconRaw = 0, linksFull = 0;
static volatile uint32_t rxB = 0, rxG = 0, rxN = 0;  // what the router sends us: 11b / 11g / 11n frames

static bool beaconActive(const Link &l) { return l.seenMs && millis() - l.seenMs < BEACON_GONE_MS; }

static bool anyBeaconActive() {
  for (int i = 1; i < MAX_LINKS; i++) if (links[i].used && beaconActive(links[i])) return true;
  return false;
}

static String linkId(int i) {
  if (i == 0) return "rtr";
  char b[8];
  snprintf(b, sizeof(b), "%02x%02x", links[i].mac[4], links[i].mac[5]);
  return b;
}

static void noteBeacon(const uint8_t *mac) {
  for (int i = 1; i < MAX_LINKS; i++) {
    if (links[i].used && memcmp(links[i].mac, mac, 6) == 0) { links[i].seenMs = millis(); return; }
  }
  for (int i = 1; i < MAX_LINKS; i++) {
    if (!links[i].used) {
      memcpy(links[i].mac, mac, 6);
      links[i].count = links[i].lastCount = 0;
      links[i].seenMs = millis();
      links[i].used = true;  // last: the CSI callback only looks at used links
      return;
    }
  }
  linksFull++;
}

// The other boards' frames arrive as ESP-NOW broadcasts. (Until 2.1.0 they were picked out of promiscuous
// mode, which hands the code every frame on the channel: with 5-7 boards sending 50/s each, the Wi-Fi driver's
// sniffer path corrupted memory and every board crashed every 1-3 minutes, 2026-11-09.)
static void espNowReceived(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (!info || !data || len < 6 || memcmp(data, "SNTLB1", 6) != 0) return;
  noteBeacon(info->src_addr);
  beaconRaw++;
}

static void csiCallback(void *ctx, wifi_csi_info_t *info) {
  if (!info || !info->buf) return;
  if (info->len < 128) { csiShort++; return; }
  csiAll++;
  int which = -1;
  for (int i = 0; i < MAX_LINKS; i++) {
    if (links[i].used && memcmp(info->mac, links[i].mac, 6) == 0) { which = i; break; }
  }
  if (which < 0) return;
  if (which == 0) { if (info->rx_ctrl.sig_mode) rxN++; else rxG++; }  // (11b frames carry no CSI: rx_b stays 0)
  // From another board, only its 50/s pulses (HT20 MCS0): its ordinary Wi-Fi traffic (replies to Sentinel,
  // pings) comes at other rates and in bursts, and would add readings that aren't comparable.
  if (which > 0 && !(info->rx_ctrl.sig_mode == 1 && info->rx_ctrl.mcs == 0 && info->rx_ctrl.cwb == 0)) {
    csiOther++;
    return;
  }
  float amp[NSUB];
  int k = 0;
  float sum = 0;
  for (int sc = 1; sc < 64; sc++) {
    if (sc >= 27 && sc <= 37) continue;  // guard band / null carriers
    int8_t im = info->buf[sc * 2], re = info->buf[sc * 2 + 1];
    amp[k] = sqrtf((float)re * re + (float)im * im);
    sum += amp[k++];
  }
  if (sum <= 0) return;
  // Divide out overall loudness: the radio's automatic gain jumps would otherwise look like motion.
  float mean = sum / NSUB;
  Link &l = links[which];
  portENTER_CRITICAL(&csiMux);
  float *slot = l.amp[l.count % WINDOW];
  for (int i = 0; i < NSUB; i++) slot[i] = amp[i] / mean;
  l.count++;
  l.rssi = info->rx_ctrl.rssi;
  portEXIT_CRITICAL(&csiMux);
}

// ---- scores ----
static void computeScores() {
  static float snap[WINDOW][NSUB];
  uint32_t now = millis();
  uint32_t elapsed = max<uint32_t>(1, now - lastScoreMs);
  lastScoreMs = now;
  for (int li = 0; li < MAX_LINKS; li++) {
    Link &l = links[li];
    if (!l.used) continue;
    uint32_t n;
    portENTER_CRITICAL(&csiMux);
    n = l.count;
    memcpy(snap, l.amp, sizeof(snap));
    portEXIT_CRITICAL(&csiMux);
    float rate = (n - l.lastCount) * 1000.0f / elapsed;
    l.lastCount = n;
    float score = -1;  // -1 = not enough readings (transmitter quiet or out of reach)
    if (n >= WINDOW && rate > 10) {
      // Mean over sub-carriers of how much each one wobbled over the window (std dev), x1000.
      float total = 0;
      for (int i = 0; i < NSUB; i++) {
        float s = 0, s2 = 0;
        for (int w = 0; w < WINDOW; w++) { s += snap[w][i]; s2 += snap[w][i] * snap[w][i]; }
        float m = s / WINDOW;
        total += sqrtf(max(0.0f, s2 / WINDOW - m * m));
      }
      score = total / NSUB * 1000.0f;
    }
    l.history[sampleSeq % HISTORY] = {sampleSeq, score, rate};
  }
  sampleSeq++;
}

// ---- router pings (generate the frames CSI is measured on) ----
static esp_ping_handle_t pingHandle = nullptr;
static bool pingPaused = false;

static void startPing() {
  if (pingHandle) return;
  IPAddress gw = WiFi.gatewayIP();
  esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
  IP_ADDR4(&cfg.target_addr, gw[0], gw[1], gw[2], gw[3]);
  cfg.count = ESP_PING_COUNT_INFINITE;
  cfg.interval_ms = PING_INTERVAL_MS;
  // Each ping waits for its reply (or this timeout) before the next goes out. The replies only matter as
  // radio frames for CSI, which is measured whether or not the ping code is still waiting, so don't let a
  // lost reply stall the stream for long (with 1 s, a weak link dropped to a handful of readings/s).
  cfg.timeout_ms = 30;
  cfg.data_size = 8;
  cfg.task_stack_size = 4096;
  esp_ping_callbacks_t cbs = {};
  if (esp_ping_new_session(&cfg, &cbs, &pingHandle) == ESP_OK) esp_ping_start(pingHandle);
}

static void stopPing() {
  if (!pingHandle) return;
  esp_ping_stop(pingHandle);
  esp_ping_delete_session(pingHandle);
  pingHandle = nullptr;
  pingPaused = false;
}

static void startCsi() {
  memcpy(links[0].mac, WiFi.BSSID(), 6);
  links[0].used = true;
  wifi_csi_config_t cfg = {};
  cfg.lltf_en = true;
  cfg.htltf_en = true;        // the first 128 bytes stay the legacy LTF we score on; these only add more after it
  cfg.stbc_htltf2_en = true;
  cfg.ltf_merge_en = true;
  cfg.channel_filter_en = false;
  cfg.manu_scale = false;
  esp_wifi_set_csi_config(&cfg);
  esp_wifi_set_csi_rx_cb(csiCallback, nullptr);
  esp_wifi_set_csi(true);
}

// ---- battery (MAX17048 fuel gauge on the Feather) ----
static bool readReg16(uint8_t reg, uint16_t &out) {
  Wire.beginTransmission(MAX17048_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MAX17048_ADDR, 2) != 2) return false;
  out = (Wire.read() << 8) | Wire.read();
  return true;
}

static String batteryJson() {
#ifndef PIN_I2C_POWER
  return "null";  // only the Feather has a fuel gauge
#endif
  uint16_t vcell, soc;
  if (!readReg16(0x02, vcell) || !readReg16(0x04, soc)) return "null";
  float volts = vcell * 78.125e-6f;
  float pct = min(100.0f, soc / 256.0f);
  return "{\"v\":" + String(volts, 3) + ",\"pct\":" + String(pct, 1) + "}";
}

// ---- status LED: colour on boards with an RGB LED (Feather, generic S3), plain on/off on the XIAO ----
static void led(uint8_t r, uint8_t g, uint8_t b) {
#if defined(RGB_BUILTIN)
  rgbLedWrite(RGB_BUILTIN, r, g, b);
#elif defined(LED_BUILTIN)
  digitalWrite(LED_BUILTIN, (r | g | b) ? LOW : HIGH);  // the XIAO's LED is lit when its pin is low
#endif
}

// "Which board is this?": GET /identify flashes the LED white for 20 s.
static uint32_t identifyUntil = 0;

static void handleIdentify() {
  identifyUntil = millis() + 20000;
  server.send(200, "application/json", "{\"ok\":true}");
}

static void identifyBlink(uint32_t now) {
  static bool lit = false;
  bool on = (int32_t)(identifyUntil - now) > 0 && now % 500 < 250;
  if (on == lit) return;
  lit = on;
  if (on) led(40, 40, 40); else led(0, 0, 0);
}

// ---- sending: the frames the other boards measure ----
struct __attribute__((packed)) BeaconFrame {
  char magic[6];  // "SNTLB1": boards only measure frames that start with this
  uint32_t seq;
};

static const uint8_t BROADCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
static volatile bool sending = false;
static volatile uint32_t framesSent = 0, sendFailed = 0;
static TaskHandle_t sendHandle = nullptr;

static void startSending() {
  if (sending) return;
  if (esp_now_init() != ESP_OK) return;
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST, 6);
  peer.channel = 0;  // = the channel the home Wi-Fi is on
  peer.ifidx = WIFI_IF_STA;
  esp_now_add_peer(&peer);
  esp_now_register_recv_cb(espNowReceived);
  esp_now_rate_config_t rate = {};
  rate.phymode = WIFI_PHY_MODE_HT20;
  rate.rate = WIFI_PHY_RATE_MCS0_LGI;  // 802.11n: carries the channel information the others need
  esp_now_set_peer_rate_config(BROADCAST, &rate);
  sending = true;
}

static void sendTask(void *) {
  // Its own task so a slow web request in loop() doesn't leave gaps in the stream.
  TickType_t last = xTaskGetTickCount();
  while (true) {
    vTaskDelayUntil(&last, pdMS_TO_TICKS(SEND_INTERVAL_MS));
    if (!sending || WiFi.status() != WL_CONNECTED) continue;
    BeaconFrame f = {{'S', 'N', 'T', 'L', 'B', '1'}, framesSent};
    if (esp_now_send(BROADCAST, (const uint8_t *)&f, sizeof(f)) == ESP_OK) framesSent++; else sendFailed++;
  }
}

// ---- Wi-Fi over USB serial: "wifi<TAB>ssid<TAB>password" ----
static void checkSerialSetup() {
  static String line;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (line.length() < 200) line += c;
      continue;
    }
    int a = line.indexOf('\t'), b = line.indexOf('\t', a + 1);
    if (line.startsWith("wifi\t") && b > a + 1) {
      prefs.putString("ssid", line.substring(a + 1, b));
      prefs.putString("pass", line.substring(b + 1));
      Serial.println("wifi saved, restarting");
      Serial.flush();
      delay(200);
      ESP.restart();
    }
    line = "";
  }
}

// ---- normal mode HTTP ----
static String samplesJson(const Link &l) {
  String js = "[";
  uint32_t first = sampleSeq > HISTORY ? sampleSeq - HISTORY : 0;
  for (uint32_t s = first; s < sampleSeq; s++) {
    const Sample &x = l.history[s % HISTORY];
    if (x.seq != s) continue;  // a link that appeared after this tick
    if (js.length() > 1) js += ",";
    js += "[" + String(x.seq) + "," + String(x.score, 2) + "," + String(x.rate, 1) + "]";
  }
  return js + "]";
}

static void handleStatus() {
  String js = "{\"id\":\"" + devId + "\",\"host\":\"" + hostName + "\",\"fw\":\"" FW_VERSION "\"";
  js += ",\"uptime_s\":" + String(millis() / 1000);
  js += ",\"wifi_rssi\":" + String(WiFi.RSSI());
  js += ",\"channel\":" + String(WiFi.channel());
  uint32_t pingSent = 0, pingReplies = 0;
  if (pingHandle) {
    esp_ping_get_profile(pingHandle, ESP_PING_PROF_REQUEST, &pingSent, sizeof(pingSent));
    esp_ping_get_profile(pingHandle, ESP_PING_PROF_REPLY, &pingReplies, sizeof(pingReplies));
  }
  js += ",\"diag\":{\"csi_all\":" + String(csiAll) + ",\"csi_short\":" + String(csiShort) + ",\"csi_other\":" + String(csiOther) +
        ",\"beacon_raw\":" + String(beaconRaw) + ",\"links_full\":" + String(linksFull) +
        ",\"ping_sent\":" + String(pingSent) + ",\"ping_replies\":" + String(pingReplies) +
        ",\"no11b\":" + String(no11b ? "true" : "false") + ",\"rx_b\":" + String(rxB) +
        ",\"rx_g\":" + String(rxG) + ",\"rx_n\":" + String(rxN) +
        ",\"sent\":" + String(framesSent) + ",\"send_failed\":" + String(sendFailed) +
        ",\"reset_reason\":" + String((int)esp_reset_reason()) +
        ",\"stack_free_loop\":" + String(uxTaskGetStackHighWaterMark(nullptr)) +
        ",\"stack_free_send\":" + String(sendHandle ? uxTaskGetStackHighWaterMark(sendHandle) : 0) +
        ",\"heap_free_min\":" + String(ESP.getMinFreeHeap()) + "}";
  js += ",\"battery\":" + batteryJson();
  // One entry per line being measured: the boards heard in the last minute, or the router when there are none.
  bool beacons = anyBeaconActive();
  js += ",\"links\":[";
  bool firstLink = true;
  for (int i = 0; i < MAX_LINKS; i++) {
    const Link &l = links[i];
    if (!l.used) continue;
    if (i == 0 ? beacons : millis() - l.seenMs > BEACON_FORGET_MS) continue;
    if (!firstLink) js += ",";
    firstLink = false;
    js += "{\"id\":\"" + linkId(i) + "\",\"source\":\"" + String(i ? "beacon" : "router") + "\",\"rssi\":" +
          String((int)l.rssi) + ",\"samples\":" + samplesJson(l) + "}";
  }
  js += "]}";
  server.send(200, "application/json", js);
}

// ---- setup mode (phone picks the home Wi-Fi) ----
static String htmlEscape(const String &s) {
  String o;
  for (char c : s) {
    if (c == '<') o += "&lt;"; else if (c == '>') o += "&gt;";
    else if (c == '&') o += "&amp;"; else if (c == '"') o += "&quot;"; else o += c;
  }
  return o;
}

static String networkOptions;

static uint32_t setupRequests = 0;

static void handleSetupPage() {
  setupRequests++;
  String h = F("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
               "<title>Sentinel sensor</title><style>body{font:18px sans-serif;margin:24px;max-width:420px}"
               "select,input,button{font-size:18px;width:100%;padding:10px;margin:6px 0 16px;box-sizing:border-box}"
               "button{background:#2563eb;color:#fff;border:0;border-radius:8px}</style>"
               "<h2>Sentinel Wi-Fi sensor</h2><p>Pick your home Wi-Fi and enter its password.</p>"
               "<form method=post action=/save><label>Network</label><select name=ssid>");
  h += networkOptions;
  h += F("</select><label>Password</label><input name=pass type=password autocomplete=off>"
         "<button>Save and connect</button></form>");
  server.send(200, "text/html", h);
}

static void handleSave() {
  String ssid = server.arg("ssid"), pass = server.arg("pass");
  if (ssid.isEmpty()) { server.send(400, "text/plain", "Pick a network"); return; }
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  server.send(200, "text/html",
              "<meta name=viewport content='width=device-width'><p style='font:18px sans-serif;margin:24px'>"
              "Saved. The sensor is joining <b>" + htmlEscape(ssid) + "</b> now; this network will disappear. "
              "You can reconnect your phone to your home Wi-Fi.</p>");
  delay(1500);
  ESP.restart();
}

static void runSetupMode() {
  setupMode = true;
  WiFi.mode(WIFI_AP_STA);
  int n = WiFi.scanNetworks();
  networkOptions = "";
  for (int i = 0; i < n; i++) {
    String s = WiFi.SSID(i);
    if (s.isEmpty() || networkOptions.indexOf(">" + htmlEscape(s) + "<") >= 0) continue;
    networkOptions += "<option>" + htmlEscape(s) + "</option>";
  }
  String apName = "Sentinel-Sensor-" + devId;
  WiFi.softAP(apName.c_str());
  dns.start(53, "*", WiFi.softAPIP());
  server.on("/", handleSetupPage);
  server.on("/save", HTTP_POST, handleSave);
  server.onNotFound([] {  // phones probe a URL to detect sign-in pages; send them to ours
    setupRequests++;
    Serial.printf("setup: %s %s\n", server.hostHeader().c_str(), server.uri().c_str());
    server.sendHeader("Location", "http://192.168.4.1/");
    server.send(302, "text/plain", "");
  });
  server.begin();
  Serial.printf("Setup mode: join Wi-Fi '%s'\n", apName.c_str());
  uint32_t start = millis(), lastNote = 0;
  while (true) {
    if (millis() - lastNote > 5000) {  // for a USB serial monitor: is anyone there, are pages being asked for?
      lastNote = millis();
      Serial.printf("setup: '%s' at %s, %d phone(s) joined, %lu page request(s), %d networks seen\n",
                    apName.c_str(), WiFi.softAPIP().toString().c_str(), WiFi.softAPgetStationNum(),
                    (unsigned long)setupRequests, n);
    }
    dns.processNextRequest();
    server.handleClient();
    checkSerialSetup();
    led(0, 0, (millis() / 500) % 2 ? 40 : 0);  // blinking blue
    // Nobody set it up in 10 min (e.g. the router was down after a power cut): try the saved Wi-Fi again.
    if (millis() - start > 600000 && WiFi.softAPgetStationNum() == 0 && prefs.isKey("ssid")) ESP.restart();
    delay(5);
  }
}

static void checkBootButton() {
  static uint32_t pressedAt = 0;
  if (digitalRead(BOOT_PIN) == LOW) {
    if (!pressedAt) pressedAt = millis();
    if (millis() - pressedAt > 3000) {
      prefs.remove("ssid");
      prefs.remove("pass");
      led(0, 0, 40);
      delay(500);
      ESP.restart();
    }
  } else {
    pressedAt = 0;
  }
}

SET_LOOP_TASK_STACK_SIZE(LOOP_STACK);  // (here, not at the top: it's a function, and Arduino puts its prototypes before the first one)

void setup() {
  Serial.begin(115200);
  pinMode(BOOT_PIN, INPUT_PULLUP);
#ifdef NEOPIXEL_POWER
  pinMode(NEOPIXEL_POWER, OUTPUT);
  digitalWrite(NEOPIXEL_POWER, NEOPIXEL_POWER_ON);
#endif
#if !defined(RGB_BUILTIN) && defined(LED_BUILTIN)
  pinMode(LED_BUILTIN, OUTPUT);
#endif
#ifdef PIN_I2C_POWER
  pinMode(PIN_I2C_POWER, OUTPUT);
  digitalWrite(PIN_I2C_POWER, HIGH);
  Wire.begin();
#endif
  prefs.begin("sensor", false);

  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);  // from eFuse: WiFi.macAddress() before Wi-Fi starts isn't reliable
  char buf[8];
  snprintf(buf, sizeof(buf), "%02x%02x", mac[4], mac[5]);
  devId = buf;
  hostName = "sentinel-motion-" + devId;

  String ssid = prefs.getString("ssid", ""), pass = prefs.getString("pass", "");
  if (ssid.isEmpty()) runSetupMode();

  led(30, 20, 0);  // yellow while connecting
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(hostName.c_str());
  WiFi.setSleep(false);  // power saving would skip frames and starve CSI
  // Leave out 802.11b: on a weak link the router otherwise falls back to 11b (DSSS/CCK) frames, which carry
  // no CSI (seen: 511 ping replies, 9 readings). Routers that insist on 11b get a normal b/g/n retry.
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
  no11b = true;
  WiFi.begin(ssid.c_str(), pass.c_str());
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    checkBootButton();
    checkSerialSetup();  // e.g. the saved network is wrong or gone
    if (no11b && millis() - start > 20000) {
      no11b = false;
      WiFi.disconnect();
      esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
      WiFi.begin(ssid.c_str(), pass.c_str());
    }
    if (millis() - start > 80000) runSetupMode();
    delay(100);
  }
  Serial.printf("Connected: %s  ch %d  rssi %d\n", WiFi.localIP().toString().c_str(), WiFi.channel(), WiFi.RSSI());
  led(0, 30, 0);
  delay(1000);
  led(0, 0, 0);

  MDNS.begin(hostName.c_str());
  MDNS.addService("sentinel-motion", "tcp", 80);
  ArduinoOTA.setHostname(hostName.c_str());
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.setMdnsEnabled(false);  // MDNS already started above
  ArduinoOTA.begin();
  server.on("/status", handleStatus);
  server.on("/identify", handleIdentify);
  server.begin();
  startCsi();
  startPing();
  startSending();
  xTaskCreate(sendTask, "beacon", SEND_STACK, nullptr, 2, &sendHandle);
  lastScoreMs = millis();
}

void loop() {
  static uint32_t lastHello = 0, lastLink = 0;
  ArduinoOTA.handle();
  server.handleClient();
  checkBootButton();
  checkSerialSetup();
  uint32_t now = millis();
  identifyBlink(now);
  if (now - lastScoreMs >= 250) computeScores();
  if (now - lastLink > 2000) {
    lastLink = now;
    if (WiFi.status() != WL_CONNECTED) {
      stopPing();
    } else if (!pingHandle) {
      startCsi();  // reconnected, maybe to a different access point
      startPing();
    } else if (anyBeaconActive() != pingPaused) {
      // The other boards' frames are all we need: stop pinging the router while one is heard (less traffic).
      pingPaused = anyBeaconActive();
      if (pingPaused) esp_ping_stop(pingHandle); else esp_ping_start(pingHandle);
    }
  }
  if (now - lastHello > 30000 || !lastHello) {
    lastHello = now;
    String hello = "{\"sentinel_sensor\":\"wifi_motion\",\"id\":\"" + devId + "\",\"host\":\"" + hostName +
                   "\",\"ip\":\"" + WiFi.localIP().toString() + "\",\"fw\":\"" FW_VERSION "\"}";
    udp.beginPacket(IPAddress(255, 255, 255, 255), HELLO_PORT);
    udp.print(hello);
    udp.endPacket();
  }
  delay(2);
}
