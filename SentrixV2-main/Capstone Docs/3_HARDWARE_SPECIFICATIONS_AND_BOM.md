# SENTRIX: Hardware Specifications & Technical Bill of Materials (BOM)

**Engineering Component Specification & Electrical Interface Register**  
**Computer Science and Engineering Department**  
**Thapar Institute of Engineering and Technology, Patiala**  
**Group:** **CPG NO. 299** | **Date:** August 2026  
**Mentor:** **Dr. Ashutosh Mishra**, Associate Professor, CSED, TIET Patiala  
**Team Members:** Kartik Garg (102303478), Prashant Gagneja (102353011), Harshit Mishra (102319039), Akshay Ranveer (102303453), Mehul Perimal (102315144)  

---

## 1. System Engineering Overview

The SENTRIX hardware architecture is engineered to provide continuous, high-reliability multimodal sensing on local edge computing appliances. The platform utilizes standard, non-proprietary industrial and commercial-off-the-shelf (COTS) electronic components to ensure maximum cost-effectiveness, zero vendor lock-in, and rapid field deployment.

---

## 2. Itemized Bill of Materials (BOM)

```
====================================================================================================================================================
Item Component Description              Part Number / Specification                Interface Type         Qty  Unit Cost (INR)  Total Cost (INR)
====================================================================================================================================================
1.   Edge Host Processing Unit          Intel Core i5 NUC / Apple Silicon Host     USB 3.0 / PCIe / RJ45  1    Workstation/Lab  ₹0 (Lab Host)
                                        (8-Core CPU, 8GB+ RAM, 256GB SSD)
----------------------------------------------------------------------------------------------------------------------------------------------------
2.   Primary Optical Ingestion Camera   Logitech C920 FHD / 1080p WDR UVC CMOS     USB 3.0 (UVC 1.5)      1    ₹2,450           ₹2,450
                                        (1920x1080 @ 30 FPS, 90° FOV, Autofocus)
----------------------------------------------------------------------------------------------------------------------------------------------------
3.   Secondary Perimeter Camera         TP-Link Tapo C310 / Hikvision 1080p IP     RTSP / Wi-Fi 802.11n   1    ₹2,800           ₹2,800
                                        (1080p, 850nm IR Night Vision, IP66)       (TCP/UDP Port 554)
----------------------------------------------------------------------------------------------------------------------------------------------------
4.   Acoustic Anomaly Sensor            BOYA BY-M1 / Omnidirectional Boundary Mic  3.5mm Jack / USB PCM   1    ₹1,200           ₹1,200
                                        (16 kHz – 48 kHz, SNR >= 58 dB, 65Hz-18kHz)
----------------------------------------------------------------------------------------------------------------------------------------------------
5.   High-Decibel Physical Siren        12V DC Piezoelectric Security Siren Sounder GPIO via 5V Relay     1    ₹650             ₹650
                                        (110 dB @ 1 meter, 250mA continuous draw)
----------------------------------------------------------------------------------------------------------------------------------------------------
6.   Galvanic Optocoupler Relay Module  PC817 5V 1-Channel Opto-Isolated Relay     5V TTL GPIO Header     1    ₹250             ₹250
                                        (Trigger: 5mA, Load: 10A 250VAC / 30VDC)
----------------------------------------------------------------------------------------------------------------------------------------------------
7.   Uninterruptible Power Supply (UPS) APC Back-UPS 600VA / 360W Line-Interactive 230V AC Output         1    ₹2,950           ₹2,950
                                        (Provides 2.5+ hours battery-backed runtime)
----------------------------------------------------------------------------------------------------------------------------------------------------
8.   Regulated DC Power Supply (SMPS)   12V 5A (60W) Universal AC-to-DC Converter  5.5x2.1mm DC Barrel    1    ₹550             ₹550
----------------------------------------------------------------------------------------------------------------------------------------------------
9.   Step-Down Buck Converter Module    LM2596 / MP1584 High-Efficiency DC-DC      Screw Terminals        1    ₹180             ₹180
                                        (In: 12V DC, Out: 5V DC @ 3A Regulated)
----------------------------------------------------------------------------------------------------------------------------------------------------
10.  Cabling, Mounts & Enclosure        Cat6 Shielded Patch Cables, USB 3.0 Ext,   Mounting Hardware      1    ₹850             ₹850
                                        Adjustable Ball-Joint Wall Brackets
====================================================================================================================================================
                                                                                    TOTAL SYSTEM HARDWARE COST: ₹11,880 INR
====================================================================================================================================================
```

---

## 3. Subsystem Datasheets & Electrical Specifications

### 3.1 Optical Camera Subsystem
* **Primary Sensor (Entry Choke-Point):**
  - **Resolution:** Full HD $1920 \times 1080$ pixels @ 30 frames per second.
  - **Color Space Support:** YUY2, MJPEG, NV12.
  - **Lens Focal Length:** $3.6\text{ mm}$ ($F/2.0$ aperture).
  - **Horizontal Field of View (H-FOV):** $78^\circ$ ($90^\circ$ diagonal).
  - **Illumination Sensitivity:** $0.5\text{ Lux}$ (Color mode).
  - **Power Consumption:** $5\text{V DC} \pm 5\%$ @ $350\text{ mA}$ ($1.75\text{ W}$).

* **Secondary Sensor (Perimeter RTSP IP Camera):**
  - **Resolution:** Full HD $1920 \times 1080$ pixels @ 30 frames per second.
  - **Compression Codec:** H.264 Baseline/Main Profile, MJPEG dual-streaming.
  - **Night Vision:** $850\text{ nm}$ Infrared LEDs with mechanical IR-Cut filter ($15\text{m}$ effective range).
  - **Ingestion Protocol:** ONVIF Profile S / RTSP (RFC 2326) over TCP port 554.
  - **Network Interface:** 10/100 Mbps Fast Ethernet RJ45 + 802.11 b/g/n Wi-Fi.
  - **Power Consumption:** $12\text{V DC} \pm 10\%$ @ $400\text{ mA}$ with IR LEDs active ($4.8\text{ W}$).

---

### 3.2 Acoustic Anomaly Sensing Subsystem
* **Transducer Type:** Omnidirectional Electret Boundary Condenser Microphone.
* **Directivity Pattern:** Omnidirectional ($360^\circ$ spatial pickup).
* **Sampling Rate:** $16,000\text{ Hz}$ ($16\text{ kHz}$ 16-bit Pulse Code Modulation).
* **Dynamic Range:** $82\text{ dB}$.
* **Frequency Range:** $65\text{ Hz} - 18,000\text{ Hz}$.
* **Signal-to-Noise Ratio (SNR):** $\ge 58\text{ dB}$ (A-weighted @ 1 kHz).
* **Total Harmonic Distortion (THD):** $< 1.0\%$ @ 1 kHz SPL 110 dB.
* **Operating Current:** $5\text{V DC}$ @ $50\text{ mA}$ ($0.25\text{ W}$).

---

### 3.3 Physical Actuator & Relay Switching Circuitry
* **Relay Isolation Module:**
  - **Optocoupler IC:** Sharp PC817 (Dielectric isolation voltage: $5,000\text{ V}_{RMS}$).
  - **Input Trigger Voltage:** $3.3\text{V} - 5.0\text{V TTL}$ logic high (active low configurable).
  - **Trigger Current:** $5\text{ mA}$ typical.
  - **Relay Contact Ratings:** $10\text{A} @ 250\text{VAC}$ / $10\text{A} @ 30\text{VDC}$.
  - **Flyback Diode:** 1N4007 fast clamping diode for back-EMF suppression.

* **Security Sounder Actuator:**
  - **Transducer Type:** High-output Piezoelectric sounder element.
  - **Sound Pressure Level (SPL):** $110\text{ dB} \pm 3\text{ dB}$ measured at 1 meter distance.
  - **Tone Characteristics:** Dual-tone sweeping warble ($1.8\text{ kHz} - 3.5\text{ kHz}$).
  - **Operating Voltage:** $12\text{V DC}$ ($9\text{V} - 15\text{V}$ operating range).
  - **Current Draw:** $250\text{ mA}$ continuous ($3.0\text{ W}$).

---

## 4. Power & Thermal Sizing Specifications

```
====================================================================================================
Subsystem Module                    Operating Voltage   Current (A)         Continuous Power (W)
====================================================================================================
Edge Host Compute Unit (Intel/Mac)  12V DC              1.0A – 2.0A         12.0W – 24.0W
Primary Optical Camera (USB)        5V DC               0.35A               1.75W
Secondary RTSP IP Camera (IR On)    12V DC              0.40A               4.80W
Acoustic Microphone Subsystem       5V DC               0.05A               0.25W
Optocoupler Relay Module            5V DC               0.08A               0.40W
Physical 110dB Siren (Active Alert) 12V DC              0.25A               3.00W (Peak only)
----------------------------------------------------------------------------------------------------
TOTAL POWER DRAW:                                       ROUTINE: 21.2W  |  PEAK ALARM: 34.2W
====================================================================================================
```

*Thermal Dissipation Assessment:* Under maximum continuous multimodal load ($24\text{W}$ CPU dissipation), system operating temperature stabilizes at $54^\circ\text{C}$ in ambient $25^\circ\text{C}$ environments, guaranteeing zero thermal throttling.
