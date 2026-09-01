#include <Wire.h>

#define INA226_ADDR 0x40
#define REG_CONFIG  0x00
#define REG_SHUNT_V 0x01
#define REG_BUS_V   0x02

void ina226Write(uint8_t reg, uint16_t val) {
  Wire.beginTransmission(INA226_ADDR);
  Wire.write(reg);
  Wire.write(val >> 8);
  Wire.write(val & 0xFF);
  Wire.endTransmission();
}

int16_t ina226Read(uint8_t reg) {
  Wire.beginTransmission(INA226_ADDR);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom((uint8_t)INA226_ADDR, (uint8_t)2);
  return (Wire.read() << 8) | Wire.read();
}

void setup() {
  Serial.begin(115200);
  Wire.begin();
  Wire.setClock(400000);

  // 1024x averaging, 140us conversion shunt+bus, continuous both
  // Avg=1024 (0b111 bit 11:9), shunt 140us, bus 140us, mode continuous
  ina226Write(REG_CONFIG, 0x4E07);

  delay(100);
  Serial.println("us,shunt,bus");
}

void loop() {
  unsigned long t = micros();
  int16_t shunt = ina226Read(REG_SHUNT_V);
  int16_t bus   = ina226Read(REG_BUS_V);

  Serial.print(t);
  Serial.print(',');
  Serial.print(shunt);
  Serial.print(',');
  Serial.println(bus);
}
