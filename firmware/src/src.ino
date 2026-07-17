#include <HX711_ADC.h>
#include <EEPROM.h>

#define VERSION "1.5"

static const uint8_t RATE_PIN     = 2;
static const uint8_t LED_PINS[3]  = {3, 5, 6};
static const uint8_t DOUT_PINS[4] = {A2, 4, A5, 10};
static const uint8_t SCK_PINS[4]  = {A3, 7, A4,  9};

// EEPROM layout
static const int EEPROM_CAL_BASE  = 0;   // 4 × float (16 bytes)
static const int EEPROM_THR_BASE  = 16;  // 4 × float (16 bytes)
static const int EEPROM_AUTOTARE  = 32;  // 1 byte
static const int EEPROM_SAMPLES   = 33;  // 1 byte

static const float DEFAULT_CAL = 14000.0f;
static const float CAL_STEP    = 10.0f;

HX711_ADC lc[4] = {
  HX711_ADC(DOUT_PINS[0], SCK_PINS[0]),
  HX711_ADC(DOUT_PINS[1], SCK_PINS[1]),
  HX711_ADC(DOUT_PINS[2], SCK_PINS[2]),
  HX711_ADC(DOUT_PINS[3], SCK_PINS[3]),
};

float   calFactor[4];
float   overloadThreshold[4];
bool    autoTareBoot  = true;
uint8_t samplesConfig = 16;
bool    diagMode      = false;

// ── EEPROM ───────────────────────────────────────────────────────────────────
static void loadCal() {
  for (uint8_t i = 0; i < 4; i++) {
    EEPROM.get(EEPROM_CAL_BASE + i * 4, calFactor[i]);
    if (isnan(calFactor[i]) || calFactor[i] == 0.0f ||
        calFactor[i] > 1e6f  || calFactor[i] < -1e6f)
      calFactor[i] = DEFAULT_CAL;
  }
}

static void saveCal(uint8_t ch) {
  EEPROM.put(EEPROM_CAL_BASE + ch * 4, calFactor[ch]);
}

static void loadThresholds() {
  for (uint8_t i = 0; i < 4; i++) {
    EEPROM.get(EEPROM_THR_BASE + i * 4, overloadThreshold[i]);
    if (isnan(overloadThreshold[i]) || overloadThreshold[i] < 0.0f)
      overloadThreshold[i] = 0.0f;
  }
}

static void saveThreshold(uint8_t ch) {
  EEPROM.put(EEPROM_THR_BASE + ch * 4, overloadThreshold[ch]);
}

static void loadSettings() {
  uint8_t at, sm;
  EEPROM.get(EEPROM_AUTOTARE, at);
  autoTareBoot = (at != 0);           // 0xFF (blank) = true (default on)

  EEPROM.get(EEPROM_SAMPLES, sm);
  bool valid = (sm == 1 || sm == 2 || sm == 4 || sm == 8 ||
                sm == 16 || sm == 32 || sm == 64 || sm == 128);
  samplesConfig = valid ? sm : 16;
}

// ── Serial output ─────────────────────────────────────────────────────────────
static void printCal() {
  char buf[48];
  int len = snprintf(buf, sizeof(buf), "CAL:%ld|%ld|%ld|%ld$\n",
    (long)(calFactor[0]*10), (long)(calFactor[1]*10),
    (long)(calFactor[2]*10), (long)(calFactor[3]*10));
  Serial.write((uint8_t*)buf, len);
}

static void printThresholds() {
  char buf[48];
  int len = snprintf(buf, sizeof(buf), "THR:%ld|%ld|%ld|%ld$\n",
    (long)(overloadThreshold[0]*100), (long)(overloadThreshold[1]*100),
    (long)(overloadThreshold[2]*100), (long)(overloadThreshold[3]*100));
  Serial.write((uint8_t*)buf, len);
}

static void printSettings() {
  char buf[32];
  int len = snprintf(buf, sizeof(buf), "AUTOTARE:%d$\n", autoTareBoot ? 1 : 0);
  Serial.write((uint8_t*)buf, len);
  len = snprintf(buf, sizeof(buf), "SAMPLES:%d$\n", samplesConfig);
  Serial.write((uint8_t*)buf, len);
}

static void printDiag() {
  char buf[48];
  int len = snprintf(buf, sizeof(buf), "RAW:%ld|%ld|%ld|%ld$\n",
    (long)(lc[0].getData() * calFactor[0]),
    (long)(lc[1].getData() * calFactor[1]),
    (long)(lc[2].getData() * calFactor[2]),
    (long)(lc[3].getData() * calFactor[3]));
  Serial.write((uint8_t*)buf, len);
}

static void adjustCal(uint8_t ch, float delta) {
  calFactor[ch] += delta;
  lc[ch].setCalFactor(calFactor[ch]);
  saveCal(ch);
  printCal();
}

// ── Command handler ───────────────────────────────────────────────────────────
static bool tarePending[4] = {};

static void handleCommand(const char* cmd) {
  uint8_t len = strlen(cmd);
  if (len == 0) return;

  switch (cmd[0]) {

    case 't':
      if (len == 1) {
        for (uint8_t i = 0; i < 4; i++) { lc[i].tareNoDelay(); tarePending[i] = true; }
      } else if (len == 2 && cmd[1] >= '0' && cmd[1] <= '3') {
        uint8_t ch = cmd[1] - '0';
        lc[ch].tareNoDelay();
        tarePending[ch] = true;
      }
      break;

    case 'p':
      diagMode = !diagMode;
      Serial.print("mode="); Serial.println(diagMode ? "DIAG" : "NORMAL");
      break;

    case 'c':
      printCal();
      printThresholds();
      printSettings();
      break;

    case 'k': {
      // k<ch>:<mass>
      if (len >= 4 && cmd[2] == ':') {
        uint8_t ch = cmd[1] - '0';
        float mass = atof(cmd + 3);
        if (ch < 4 && mass > 0.0f) {
          float reading = lc[ch].getData();
          float signedMass = (reading < 0.0f) ? -mass : mass;
          calFactor[ch] = lc[ch].getNewCalibration(signedMass);
          saveCal(ch);
          printCal();
        }
      }
      break;
    }

    case 'v': {
      // v<ch>:<value>  set cal factor directly
      if (len >= 4 && cmd[2] == ':') {
        uint8_t ch = cmd[1] - '0';
        float val = atof(cmd + 3);
        if (ch < 4 && val != 0.0f) {
          calFactor[ch] = val;
          lc[ch].setCalFactor(calFactor[ch]);
          saveCal(ch);
          printCal();
        }
      }
      break;
    }

    case 'o': {
      // o<ch>:<threshold>
      if (len >= 4 && cmd[2] == ':') {
        uint8_t ch = cmd[1] - '0';
        float thr = atof(cmd + 3);
        if (ch < 4 && thr >= 0.0f) {
          overloadThreshold[ch] = thr;
          saveThreshold(ch);
        }
      }
      break;
    }

    case 'a':
      // at0 / at1 — auto-tare on boot
      if (len == 3 && cmd[1] == 't') {
        autoTareBoot = (cmd[2] == '1');
        EEPROM.put(EEPROM_AUTOTARE, (uint8_t)(autoTareBoot ? 1 : 0));
        printSettings();
      } else {
        adjustCal(0, -CAL_STEP);
      }
      break;

    case 's':
      // sm<val> — set samples in use (1,2,4,8,16,32,64,128)
      if (len >= 3 && cmd[1] == 'm') {
        int s = atoi(cmd + 2);
        bool valid = (s==1||s==2||s==4||s==8||s==16||s==32||s==64||s==128);
        if (valid) {
          samplesConfig = (uint8_t)s;
          for (uint8_t i = 0; i < 4; i++) {
            lc[i].setSamplesInUse(s);
            lc[i].resetSamplesIndex();
          }
          EEPROM.put(EEPROM_SAMPLES, samplesConfig);
          printSettings();
        }
      } else {
        adjustCal(1, -CAL_STEP);
      }
      break;

    case 'q': adjustCal(0, +CAL_STEP); break;
    case 'w': adjustCal(1, +CAL_STEP); break;
    case 'e': adjustCal(2, +CAL_STEP); break;
    case 'd': adjustCal(2, -CAL_STEP); break;
    case 'r': adjustCal(3, +CAL_STEP); break;
    case 'f': adjustCal(3, -CAL_STEP); break;
  }
}

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
  pinMode(RATE_PIN, OUTPUT);
  for (uint8_t i = 0; i < 3; i++) {
    pinMode(LED_PINS[i], OUTPUT);
    digitalWrite(LED_PINS[i], HIGH);
  }
  delay(1000);
  for (uint8_t i = 0; i < 3; i++) digitalWrite(LED_PINS[i], LOW);
  digitalWrite(RATE_PIN, LOW);   // 10 SPS (more accurate; was HIGH = 80 SPS)

  Serial.begin(115200);
  Serial.println("LBV1 v" VERSION);

  loadCal();
  loadThresholds();
  loadSettings();

  for (uint8_t i = 0; i < 4; i++) lc[i].begin();

  byte rdy[4] = {};
  uint8_t readyCount = 0;
  while (readyCount < 4) {
    readyCount = 0;
    for (uint8_t i = 0; i < 4; i++) {
      if (!rdy[i]) rdy[i] = lc[i].startMultiple(2000, autoTareBoot);
      readyCount += rdy[i];
    }
  }

  for (uint8_t i = 0; i < 4; i++) {
    lc[i].setCalFactor(calFactor[i]);
    lc[i].setSamplesInUse(samplesConfig);
  }

  printCal();
  printThresholds();
  printSettings();
}

// ── Loop ──────────────────────────────────────────────────────────────────────
void loop() {
  static bool     ch_rdy[4]      = {};
  static bool     prevOvl[4]     = {};
  static bool     currOvl[4]     = {};
  static bool     ledState       = false;
  static unsigned long lastBlink = 0;

  // Poll HX711
  for (uint8_t i = 0; i < 4; i++) {
    if (lc[i].update()) ch_rdy[i] = true;
  }

  // Tare complete notifications
  for (uint8_t i = 0; i < 4; i++) {
    if (tarePending[i] && lc[i].getTareStatus()) {
      tarePending[i] = false;
      char buf[20];
      int len = snprintf(buf, sizeof(buf), "TARE_DONE:%d$\n", i);
      Serial.write((uint8_t*)buf, len);
    }
  }

  // Output when all channels fresh
  if (ch_rdy[0] & ch_rdy[1] & ch_rdy[2] & ch_rdy[3]) {

    if (diagMode) {
      printDiag();
    } else {
      char buf[48];
      int len = snprintf(buf, sizeof(buf), "%ld|%ld|%ld|%ld$\n",
        -(long)(lc[0].getData() * 100.0f),
        -(long)(lc[1].getData() * 100.0f),
        -(long)(lc[2].getData() * 100.0f),
        -(long)(lc[3].getData() * 100.0f));
      Serial.write((uint8_t*)buf, len);
    }

    // Overload — send OVL: only on state change
    bool changed = false;
    for (uint8_t i = 0; i < 4; i++) {
      currOvl[i] = (overloadThreshold[i] > 0.0f &&
                    fabsf(lc[i].getData()) > overloadThreshold[i]);
      if (currOvl[i] != prevOvl[i]) changed = true;
    }
    if (changed) {
      char buf[24];
      int len = snprintf(buf, sizeof(buf), "OVL:%d|%d|%d|%d$\n",
        currOvl[0], currOvl[1], currOvl[2], currOvl[3]);
      Serial.write((uint8_t*)buf, len);
      memcpy(prevOvl, currOvl, 4);
    }

    ch_rdy[0] = ch_rdy[1] = ch_rdy[2] = ch_rdy[3] = false;
  }

  // Overload LED blink
  bool anyOvl = prevOvl[0] | prevOvl[1] | prevOvl[2] | prevOvl[3];
  unsigned long now = millis();
  if (anyOvl) {
    if (now - lastBlink >= 200) { ledState = !ledState; digitalWrite(LED_PINS[2], ledState); lastBlink = now; }
  } else if (ledState) {
    ledState = false; digitalWrite(LED_PINS[2], LOW);
  }

  // Line-buffered command parser
  static char    cmdBuf[32];
  static uint8_t cmdLen = 0;
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdLen > 0) { cmdBuf[cmdLen] = '\0'; handleCommand(cmdBuf); cmdLen = 0; }
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = c;
    }
  }
}
