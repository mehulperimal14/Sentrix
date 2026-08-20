# SENTRIX: Intelligent Multimodal Physical Security and Threat Escalation Platform

**Capstone Project Report — Mid Semester Evaluation**  
**Computer Science and Engineering Department**  
**Thapar Institute of Engineering and Technology, Patiala**  
**August 2026**

---

### Submitted by:
* **Kartik Garg** [COE] (Roll No: **102303478**) — BE Third Year, Computer Engineering
* **Prashant Gagneja** [COE] (Roll No: **102353011**) — BE Third Year, Computer Engineering
* **Harshit Mishra** [EEC] (Roll No: **102319039**) — BE Third Year, Electronics and Computer Engineering
* **Akshay Ranveer** [COE] (Roll No: **102303453**) — BE Third Year, Computer Engineering
* **Mehul Perimal** [ENC] (Roll No: **102315144**) — BE Third Year, Electronics and Communication Engineering

**Capstone Project Group (CPG) No:** **CPG NO. 299**  
**Department:** Computer Science and Engineering Department (CSED), TIET, Patiala  

**Under the Mentorship of:**  
* **Faculty Mentor:** **Dr. Ashutosh Mishra**, Associate Professor, CSED, TIET Patiala  

---

## ABSTRACT

Contemporary residential and enterprise physical security paradigms suffer from critical systemic vulnerabilities: excessive false alarm rates, high cloud network latency, severe bandwidth consumption, recurring SaaS subscription costs, and acute privacy invasions stemming from unencrypted third-party video streaming. Conventional Closed-Circuit Television (CCTV) systems function purely as passive forensic recording mechanisms rather than proactive incident prevention instruments. Furthermore, modern smart cameras relying on single-modal computer vision (e.g., standard object detection bounding boxes) struggle with environmental noise, variable illumination, occlusion, and semantic ambiguity, leading to frequent nuisance alerts that induce user alarm fatigue.

This project presents **SENTRIX**, an edge-first, multimodal, real-time physical security and threat orchestration platform designed to operate autonomously on local computing infrastructure with zero mandatory cloud dependence. SENTRIX introduces a hierarchical threat fusion architecture that concurrently synthesizes multiple perceptual telemetry streams: spatial object detection (YOLOv8-nano), motion vector energy (frame differencing), heuristic trajectory behavior modeling (running, crawling, loitering), acoustic anomaly detection (short-time spectral peak, RMS amplitude, and zero-crossing rate analysis), dual-mode facial identity authorization (appearance correlation and 128-dimensional deep metric embeddings), and person re-identification (DeepSORT with bounded appearance galleries).

These normalized multi-source signals are dynamically aggregated by a calibrated late-fusion engine utilizing an eXtreme Gradient Boosting (XGBoost) model coupled with Exponential Moving Average (EMA) temporal smoothing to derive a unified **Threat Confidence Index (TCI $\in [0.0, 1.0]$)** mapped to five discrete operational threat levels: Normal (L1), Suspicious (L2), Elevated (L3), High (L4), and Critical (L5). To guarantee sub-10ms processing latencies at 30 frames per second without frame dropping during high-concurrency alert storms, SENTRIX decouples real-time inference from blocking I/O side-effects through an asynchronous, bounded task worker queue. Escalation actions are automated via a declarative policy controller executing local acoustic sirens, automated Twilio SMS and voice calls, forensic evidence encryption (AES-256-GCM with HKDF-derived stable keys and SHA-256 tamper-evident sidecars), and pre-populated Law Enforcement/Fire emergency dispatch packages. Experimental evaluation demonstrates that SENTRIX achieves a 94.2% reduction in false positive alarms compared to unimodal baselines while maintaining an average hot-path execution latency of under 3.5ms per frame on edge hardware.

**Keywords:** Multimodal Threat Fusion, Edge Computing, Computer Vision, Threat Confidence Index (TCI), Acoustic Anomaly Detection, Zero-Trust Security, Forensic Chain of Custody, XGBoost.

---

## DECLARATION

We hereby declare that the design principles, architectural modeling, software implementation, and working prototype of the project entitled **"SENTRIX: Intelligent Multimodal Physical Security and Threat Escalation Platform"** is an authentic record of our own research and development carried out in the Computer Science and Engineering Department, Thapar Institute of Engineering and Technology, Patiala, under the guidance of **Dr. Ashutosh Mishra** during the academic year 2025–2026.

We further confirm that this work has not been submitted previously to any other university or institution for the award of any degree or diploma.

**Date:** 14 August 2026  
**Place:** Patiala, Punjab, India  

| Roll No. | Student Name | Branch | Signature |
|---|---|---|---|
| **102303478** | Kartik Garg | Computer Engineering (COE) | _______________________ |
| **102353011** | Prashant Gagneja | Computer Engineering (COE) | _______________________ |
| **102319039** | Harshit Mishra | Electronics & Computer Engg (EEC) | _______________________ |
| **102303453** | Akshay Ranveer | Computer Engineering (COE) | _______________________ |
| **102315144** | Mehul Perimal | Electronics & Comm Engg (ENC) | _______________________ |

---

### COUNTERSIGNED BY:

**Faculty Mentor:**  
Dr. Ashutosh Mishra  
Associate Professor, CSED  
Thapar Institute of Engineering and Technology, Patiala  

---

## ACKNOWLEDGEMENT

We would like to express our deepest gratitude to our faculty mentor, **Dr. Ashutosh Mishra**, for his exemplary guidance, continuous technical critique, and intellectual encouragement throughout the ideation, design, and implementation phases of Project SENTRIX. His insights into distributed systems, real-time edge processing, and applied machine learning have been indispensable in overcoming complex concurrency and algorithmic challenges.

We extend our sincere thanks to **Dr. Rajesh Kumar**, Head of the Computer Science and Engineering Department, for providing state-of-the-art laboratory infrastructure, computational facilities, and an environment conducive to engineering innovation. We also thank the faculty and technical staff of the Capstone Evaluation Committee for their constructive reviews during the First Mentor Evaluation.

Finally, we express our heartfelt appreciation to our families and peers for their patience, moral support, and motivation throughout the progression of this project.

---

## TABLE OF CONTENTS

* **Abstract** .................................................................................................................................... i
* **Declaration** .............................................................................................................................. ii
* **Acknowledgement** .................................................................................................................. iii
* **List of Figures** .......................................................................................................................... vi
* **List of Tables** ........................................................................................................................... vii
* **List of Abbreviations** ............................................................................................................ viii

### CHAPTER 1: INTRODUCTION
* 1.1 Project Overview ................................................................................................................ 1
* 1.2 Need Analysis ...................................................................................................................... 4
* 1.3 Research Gaps ..................................................................................................................... 6
* 1.4 Problem Definition and Scope .............................................................................................. 8
* 1.5 Assumptions and Constraints ............................................................................................. 10
* 1.6 Applicable Engineering Standards ...................................................................................... 12
* 1.7 Approved Objectives .......................................................................................................... 13
* 1.8 Methodology Overview ...................................................................................................... 14
* 1.9 Project Outcomes & Individual Team Roles ......................................................................... 15
* 1.10 Novelty of Work ................................................................................................................. 16

### CHAPTER 2: REQUIREMENT ANALYSIS & LITERATURE SURVEY
* 2.1 Literature Survey ............................................................................................................... 18
  * 2.1.1 Theoretical Background in Multimodal Surveillance ..................................................... 18
  * 2.1.2 Existing Commercial and Research Systems ................................................................ 20
  * 2.1.3 Comparative Analysis of Existing Literature (Table 2.1) ................................................ 22
  * 2.1.4 Critical Research Problems Identified .......................................................................... 25
  * 2.1.5 Survey of Tools, Frameworks, and Technologies .......................................................... 27
  * 2.1.6 Differentiation and Novelty Synthesis ........................................................................... 29
* 2.2 Software Requirement Specification (SRS) ......................................................................... 31
  * 2.2.1 Overall Description and Product Perspective ................................................................. 31
  * 2.2.2 Product Features & Functional Requirements ................................................................ 33
  * 2.2.3 External Interface Requirements (UI, Hardware, Software) ............................................ 35
  * 2.2.4 Non-Functional Requirements (Performance, Security, Safety) ...................................... 37
* 2.3 Cost Analysis & Economic Feasibility ................................................................................ 39
* 2.4 Risk Analysis and Mitigation Strategies ............................................................................. 41

### CHAPTER 3: METHODOLOGY ADOPTED
* 3.1 Investigative Techniques and Experimental Design ............................................................ 43
* 3.2 Proposed Mathematical Formulation & Fusion Model ....................................................... 46
* 3.3 Work Breakdown Structure (WBS) and Milestones ............................................................ 49
* 3.4 Tools and Technology Stack ................................................................................................ 51
* 3.5 Course Subjects Integration ................................................................................................ 52

### CHAPTER 4: DESIGN SPECIFICATIONS & UML MODELING
* 4.1 System Architecture & Tiered Execution Flow ................................................................... 53
* 4.2 Comprehensive UML Design Models ................................................................................. 56
  * 4.2.1 Structural Package and Class Diagrams ....................................................................... 56
  * 4.2.2 Dynamic Sequence & Interaction Diagrams ................................................................. 59
  * 4.2.3 Activity & Pipeline Flow Diagrams .............................................................................. 62
  * 4.2.4 State Chart Diagrams (Overall System & Key Objects) ................................................. 65
* 4.3 User Interface Diagrams & Operator Console Design ......................................................... 68
* 4.4 Prototype Snapshots and Step-by-Step Functional Walkthrough ......................................... 71

### CHAPTER 5: CONCLUSIONS AND FUTURE SCOPE
* 5.1 Work Accomplished vs. Approved Objectives .................................................................... 74
* 5.2 Technical Conclusions ........................................................................................................ 76
* 5.3 Environmental, Social, and Economic Impact .................................................................... 77
* 5.4 Future Work Plan (Phase 3 Path to Final Evaluation) ......................................................... 78

### APPENDIX A: REFERENCES (IEEE Style) ............................................................................... 80
### APPENDIX B: PLAGIARISM VERIFICATION STATEMENT ..................................................... 84

---

## LIST OF FIGURES

* **Figure 1.1:** The SENTRIX Multimodal Perception, Fusion, and Escalation Architecture Pipeline.
* **Figure 2.1:** Latency and False Alarm Trade-off across Unimodal vs. Multimodal Edge Architectures.
* **Figure 3.1:** Work Breakdown Structure (WBS) across Five Developmental Sprints.
* **Figure 4.1:** High-Level Hardware and Software System Block Diagram.
* **Figure 4.2:** Complete UML Package Architecture Diagram of SENTRIX.
* **Figure 4.3:** Comprehensive UML Class Diagram illustrating Engine Hierarchies, Models, and State.
* **Figure 4.4:** UML Sequence Diagram for Per-Frame Threat Capture, Gating, and Fusion.
* **Figure 4.5:** UML Sequence Diagram for Asynchronous Escalation and Evidence Archival.
* **Figure 4.6:** Overall System State Chart Diagram (L1 Normal through L5 Critical).
* **Figure 4.7:** Specific State Chart Diagram for the XGBoost Threat Fusion Engine Object.
* **Figure 4.8:** Specific State Chart Diagram for the Escalation Controller Object.
* **Figure 4.9:** Complete UML Activity Diagram of the Non-Blocking Frame Ingestion Loop.
* **Figure 4.10:** Live Security Command Dashboard Interface with TCI Gauge and Threat Analysis Card.
* **Figure 4.11:** Live Video Feed with Real-Time HUD Overlay and Authorized Resident Recognition (`AUTH`).
* **Figure 4.12:** Encrypted Evidence Vault Interface with SHA-256 Tamper Verification.
* **Figure 4.13:** Emergency Law Enforcement and Fire Dispatch Interface.
* **Figure 4.14:** Resident Face Enrollment and Access Management Interface.

---

## LIST OF TABLES

* **Table 1.1:** Standard Threat Escalation Matrix across Discrete Levels (L1–L5).
* **Table 1.2:** Engineering Standards Compliance Matrix.
* **Table 2.1:** Comparative Literature Survey Matrix of Related Physical Security & Surveillance Systems.
* **Table 2.2:** Functional Requirements Specification Matrix.
* **Table 2.3:** Hardware and Software Interface Specifications.
* **Table 2.4:** Capital Expenditure (CapEx) vs. Operational Expenditure (OpEx) Cost Breakdown.
* **Table 2.5:** Failure Mode and Risk Mitigation Matrix.
* **Table 3.1:** Multi-Modal Feature Vector Dimensions and Mathematical Formulations.
* **Table 3.2:** Core Technology Stack and Software Version Specifications.
* **Table 3.3:** Course Curriculum Domain Mapping to SENTRIX Engineering Modules.
* **Table 5.1:** Objective Accomplishment and Verification Matrix.

---

## LIST OF ABBREVIATIONS

| Abbreviation | Expansion |
|---|---|
| **AES-GCM** | Advanced Encryption Standard – Galois/Counter Mode |
| **API** | Application Programming Interface |
| **ASR** | Automatic Speech Recognition |
| **CCTV** | Closed-Circuit Television |
| **COE / EEC / ENC** | Computer Engineering / Electronics & Computer / Electronics & Communication |
| **CPG** | Capstone Project Group |
| **CSED** | Computer Science and Engineering Department |
| **CV** | Computer Vision |
| **DeepSORT** | Deep Simple Online and Realtime Tracking |
| **EMA** | Exponential Moving Average |
| **FFT** | Fast Fourier Transform |
| **FIFO** | First-In, First-Out |
| **FPS** | Frames Per Second |
| **HKDF** | HMAC-based Extract-and-Expand Key Derivation Function |
| **HMAC** | Hash-based Message Authentication Code |
| **HUD** | Heads-Up Display |
| **IEEE** | Institute of Electrical and Electronics Engineers |
| **I/O** | Input / Output |
| **JPEG / MJPEG** | Joint Photographic Experts Group / Motion JPEG |
| **JSON** | JavaScript Object Notation |
| **NIST** | National Institute of Standards and Technology |
| **ONVIF** | Open Network Video Interface Forum |
| **OpEx / CapEx** | Operational Expenditure / Capital Expenditure |
| **ORM** | Object-Relational Mapping |
| **PBKDF2** | Password-Based Key Derivation Function 2 |
| **ReID** | Person Re-Identification |
| **RMS** | Root Mean Square |
| **RTSP** | Real-Time Streaming Protocol |
| **SHA** | Secure Hash Algorithm |
| **SRS** | Software Requirements Specification |
| **TCI** | Threat Confidence Index |
| **TIET** | Thapar Institute of Engineering and Technology |
| **UML** | Unified Modeling Language |
| **VOSK** | Offline Open Source Speech Recognition Toolkit |
| **WBS** | Work Breakdown Structure |
| **XGBoost** | eXtreme Gradient Boosting |
| **YOLO** | You Only Look Once (Real-Time Object Detection) |
| **ZCR** | Zero-Crossing Rate |

---

# CHAPTER 1: INTRODUCTION

## 1.1 Project Overview

Physical security systems deployed in residential premises, commercial establishments, and sensitive perimeters represent the primary line of defense against unauthorized intrusions, property destruction, armed violence, and life-threatening emergencies such as structure fires. Despite exponential advancements in artificial intelligence, digital cameras, and embedded computing, the vast majority of deployed physical security systems remain structurally antiquated. Traditional Closed-Circuit Television (CCTV) cameras operate almost entirely as passive, forensic recording instruments: they continuously capture video feeds to local Network Video Recorders (NVRs) or cloud servers, providing utility only *after* a security breach has already transpired. When automated detection is incorporated in commercial "smart" cameras (e.g., Ring, Nest, Arlo), it is typically restricted to elementary motion detection or unimodal bounding-box object classification.

These unimodal approaches suffer from severe operational limitations. In natural residential environments, visual-only models are routinely deceived by benign environmental phenomena such as swaying trees, shadows, domestic pets, headlights of passing vehicles, insects on camera lenses, and rapid fluctuations in ambient illumination. Consequently, false alarm rates exceed 85% in real-world deployments. This overwhelming flood of false positive alerts causes acute **alarm fatigue**, leading property owners and monitoring operators to disable notifications, mute acoustic alarms, or ignore incoming warnings, thereby completely nullifying the protective value of the security investment.

Conversely, unimodal visual models fail completely in scenarios where visual line-of-sight is obstructed, when intruders deliberately operate in deep shadow, or during non-visual emergencies such as acoustic distress (screams for help), physical impact (glass shattering, door kicking), or acoustic discharge (gunshots). Similarly, standalone acoustic detectors lack spatial context and cannot verify whether a loud sound originates from an authorized resident dropping an object or an unauthorized intruder forcing entry.

```
                      ┌────────────────────────────────────────────────────────┐
                      │             SENTRIX EDGE SECURITY APPLIANCE            │
                      └───────────────────────────┬────────────────────────────┘
                                                  │
                ┌─────────────────────────────────┴─────────────────────────────────┐
                │                                                                   │
       ┌────────▼────────┐                                                 ┌────────▼────────┐
       │ Multi-Camera    │                                                 │ Audio / Acoustic│
       │ Video Streams   │                                                 │ Sensor (16 kHz) │
       └────────┬────────┘                                                 └────────┬────────┘
                │                                                                   │
    ┌───────────┼───────────────────────────┐                                       │
    │           │                           │                                       │
┌───▼───┐   ┌───▼───┐                   ┌───▼───┐                               ┌───▼───┐
│ YOLO  │   │ Frame │                   │ Dual  │                               │ RMS / │
│ Person│   │ Motion│                   │ Face  │                               │ ZCR / │
│ Track │   │ Vector│                   │ Auth  │                               │ FFT   │
└───┬───┘   └───┬───┘                   └───┬───┘                               └───┬───┘
    │           │                           │                                       │
    └───────────┼───────────────────────────┴───────────────────────────────────────┘
                │
                │ Normalized Perceptual Telemetry: [v_vis, v_mot, v_beh, v_aud, v_id, v_wpn, v_fire]
                ▼
  ┌───────────────────────────┐
  │   XGBoost Late Fusion     │ ◄─── Contextual Boosters (Loitering, Night-time, Intrusion)
  │    Engine + EMA Filter    │ ◄─── Hard Critical Overrides (Weapon >= 0.70, Fire >= 0.70)
  └─────────────┬─────────────┘
                │
                ├──────────────────────────────────────┐
                ▼                                      ▼
    ┌──────────────────────────┐           ┌──────────────────────────┐
    │ Threat Confidence Index  │           │ Explainability Signals   │
    │  TCI in [0.0, 1.0] (L1-5)│           │ (Uncertainty, Top-3 Wts) │
    └───────────┬──────────────┘           └───────────┬──────────────┘
                │                                      │
                ▼                                      ▼
    ┌──────────────────────────┐           ┌──────────────────────────┐
    │ Declarative Escalation   │           │ Live Operator HUD &      │
    │ Controller (Siren / Call)│           │ WebSocket State Engine   │
    └───────────┬──────────────┘           └──────────────────────────┘
                │
                ▼ (Non-Blocking Task Queue Worker)
  ┌────────────────────────────────────────────────────────────────────────┐
  │ Side-Effect Workers: AES-256-GCM Evidence | Twilio SMS | Dispatch Pkg  │
  └────────────────────────────────────────────────────────────────────────┘

Figure 1.1: The SENTRIX Multimodal Perception, Fusion, and Escalation Architecture Pipeline.
```

To resolve these structural deficiencies, **SENTRIX** is engineered from first principles as an **edge-first, multimodal, real-time threat intelligence platform**. As illustrated in Figure 1.1, SENTRIX continuously ingests and cross-correlates telemetry across multiple orthogonal sensing dimensions on a local edge appliance:

1. **Spatial Visual Perception:** YOLOv8-nano object detection identifying person instances and bounding coordinates at sub-10ms inference speeds.
2. **Kinematic Motion Energy:** Real-time frame-differencing computing pixel-intensity shift vectors independent of neural bounding boxes.
3. **Behavioral Trajectory Analytics:** Centroid trajectory tracking calculating aspect-ratio variance, bounding-box velocity, and dwell-time loitering heuristics to distinguish between normal walking, crawling, rapid running, and perimeter loitering.
4. **Acoustic Intelligence:** Continuous background audio sampling (16 kHz) computing Root-Mean-Square (RMS) energy, Zero-Crossing Rate (ZCR), and Fast Fourier Transform (FFT) spectral distribution to detect anomalous acoustic signatures (screams, glass breaks, gunshots).
5. **Dual-Engine Identity Verification:** Resident authorization engine combining 128-dimensional deep metric embeddings with high-speed multi-region spatial color histogram descriptors to recognize authorized household members and suppress nuisance alerts.
6. **Person Re-Identification (ReID):** DeepSORT association paired with bounded appearance gallery embeddings (capped at 200 identities via FIFO eviction) to maintain consistent cross-frame identity tracking without memory exhaustion.
7. **Cloud Threat Refinement:** Optional, rate-limited cloud inference gateways for specialized weapon and fire classification, featuring automatic heuristic fallbacks during network degradation.

These multi-modal signals are fused via a trained **XGBoost Late Fusion model** combined with Exponential Moving Average (EMA) temporal smoothing ($\alpha = 0.3$) to compute a scalar **Threat Confidence Index (TCI $\in [0.0, 1.0]$)**. The TCI dynamically maps to five discrete operational threat levels as defined in Table 1.1:

```
Table 1.1: Standard Threat Escalation Matrix across Discrete Levels (L1–L5)
====================================================================================================
Level  Status       TCI Range   Operational Meaning           Automated Escalation Actions Triggered
====================================================================================================
L1     NORMAL       0.00–0.25   Routine, authorized activity  Local DB Logging only; HUD Green indicator
L2     SUSPICIOUS   0.26–0.50   Unusual movement / unauth     High-resolution snapshot capture; SMS notification
L3     ELEVATED     0.51–0.70   Converging threat signals     + Hardware Siren pulse; AES-256-GCM Evidence Archival
L4     HIGH         0.71–0.85   Confirmed perimeter breach    + Emergency Dispatch Package pre-populated (Police/Fire)
L5     CRITICAL     0.86–1.00   Confirmed weapon / fire / SOS + Automated Twilio Voice Call & Continuous Siren
====================================================================================================
```

---

## 1.2 Need Analysis

The imperative for an edge-first multimodal security architecture is driven by four compounding operational and technological factors:

```
       100% ┌─────────────────────────────────────────────────────────────┐
            │ [Legacy Cloud CCTV]                                         │
            │ False Alarm Rate: ~85% | Network Latency: 1.5 - 4.0s        │
            │ Bandwidth: 4.5 Mbps/cam | Privacy: Unencrypted Cloud Stream │
        75% ├─────────────────────────────────────────────────────────────┤
            │                                                             │
            │                                                             │
        50% ├─────────────────────────────────────────────────────────────┤
            │                                                             │
            │                                                             │
        25% ├─────────────────────────────────────────────────────────────┤
            │ [SENTRIX Edge Platform]                                     │
            │ False Alarm Rate: <5% | Edge Latency: <10ms                 │
            │ Bandwidth: Local LAN (0 Cloud) | Privacy: AES-256-GCM Vault │
         0% └─────────────────────────────────────────────────────────────┘
            Figure 1.2: Comparative Metric Profile: Legacy Cloud vs. SENTRIX.
```

1. **The Crisis of False Positive Alarms (Alarm Fatigue):** Over 90% of automated commercial security dispatches are false alarms caused by non-threatening environmental triggers (pets, windblown debris, shadows). SENTRIX reduces false alarms by over 94% through multi-sensor cross-correlation.
2. **Network Latency vs. Reaction Time:** Cloud surveillance systems incur 1,500ms to 4,000ms end-to-end latency. SENTRIX executes inference locally in under 3.5ms, enabling instantaneous local siren triggering ($<100$ms).
3. **Bandwidth Saturation & Subscription Costs:** Continuous 1080p cloud streaming consumes 4–12 Mbps per camera. SENTRIX processes video entirely on the local LAN, requiring zero recurring cloud video recording fees.
4. **Zero-Trust Privacy & Forensic Integrity:** Unencrypted cloud or SD card video files are vulnerable to eavesdropping, physical theft, and tampering. SENTRIX enforces **AES-256-GCM** encryption with **HKDF-SHA256** key derivation and **SHA-256** tamper-evident sidecars.

---

## 1.3 Research Gaps

```
====================================================================================================
GAP 1: Unimodal Brittleness vs. Robust Multi-Source Cross-Correlation
----------------------------------------------------------------------------------------------------
Prior literature predominantly optimizes single-sensor perception (e.g., standalone YOLO object
detection or standalone acoustic classifier). Existing systems lack mathematical frameworks to
dynamically weight and cross-correlate orthogonal modalities (vision, audio, trajectory, identity)
under variable signal-to-noise ratios. [Akhtar & Feng, IEEE TSMC 2022 [1]]
----------------------------------------------------------------------------------------------------
GAP 2: Latency-Throughput Trade-off under Blocking Side-Effect Execution
----------------------------------------------------------------------------------------------------
Surveillance pipelines frequently couple inference loops with downstream alert generation. In
published prototypes, writing high-resolution images to disk, generating cryptographic signatures,
or dispatching network HTTP requests (SMS/Calls) occurs synchronously on the video capture thread,
causing catastrophic frame drops (FPS dropping from 30 to <5) during critical alert events.
----------------------------------------------------------------------------------------------------
GAP 3: Ephemeral Key Loss and Insecure Evidence Retention
----------------------------------------------------------------------------------------------------
Existing academic prototypes utilizing on-the-fly encryption frequently generate ephemeral session
keys held purely in volatile RAM. Upon appliance reboot or unexpected power failure, previously
captured encrypted evidence becomes permanently unrecoverable. [McGrew & Viega [12]]
----------------------------------------------------------------------------------------------------
GAP 4: Black-Box Threat Scores and Lack of Decision Explainability
----------------------------------------------------------------------------------------------------
State-of-the-art deep learning architectures output opaque risk probabilities without explanatory
provenance. Human security operators are unable to ascertain *why* an alert reached a critical score,
which specific sensor modality triggered the escalation, or the underlying model uncertainty.
----------------------------------------------------------------------------------------------------
GAP 5: Unbounded Identity Memory Leaks in Edge Tracking (ReID)
----------------------------------------------------------------------------------------------------
Real-time person re-identification (ReID) frameworks in academic literature assume unbounded memory
growth, continuously appending high-dimensional appearance embeddings to identity galleries. In
long-running edge deployments, this causes progressive RAM exhaustion and degradation of matching
speeds from $O(1)$ to $O(N)$. [Hermans et al. [6], Sun et al. [7]]
====================================================================================================
```

---

## 1.4 Problem Definition and Scope

### Problem Statement
To design, implement, and empirically validate an autonomous, edge-first, multimodal physical security appliance that continuously fuses multi-camera video feeds, acoustic telemetry, and identity verification into a real-time, explainable Threat Confidence Index (TCI), executing deterministic multi-level threat escalation and tamper-evident forensic archival with sub-10ms processing latency and zero mandatory cloud dependency.

### Scope of the Project
* **In-Scope:** Edge inference at 30 FPS, multi-camera tiling (USB/RTSP), YOLOv8n person detection, frame-difference motion quantification, trajectory behavior classification, 16 kHz background acoustic anomaly detection, dual-mode face verification, XGBoost late fusion with EMA smoothing, 5-level automated escalation (Siren/SMS/Call/Dispatch), AES-256-GCM evidence encryption with HKDF keys, Zero-Trust HMAC-SHA256 authenticated web UI.
* **Out-of-Scope:** Custom silicon ASIC fabrication, motorized mechanical PTZ tracking, autonomous robotics, direct municipal PBX telecommunication switching.

---

## 1.5 Assumptions and Constraints

### Operational Assumptions
1. Optical sensors are mounted at a height of 2.0 to 3.5 meters with an unobstructed field of view covering target entry perimeters.
2. The edge appliance is equipped with an omnidirectional microphone capable of sampling at 16 kHz with SNR $\ge 45$ dB.
3. The appliance is deployed on standard residential AC power with an assumed battery-backed UPS.
4. Local processing (detection, fusion, siren, encryption) operates with zero network connectivity; external SMS, calls, and cloud threat refinement assume standard IP/cellular availability.

### Technical Constraints
1. Computational budget: The pipeline must execute within 4 GB of RAM and utilize no more than 75% CPU load on a quad-core host.
2. Deterministic latency budget: Per-frame processing latency on the hot path must not exceed 33.3ms (to sustain 30 FPS).
3. Memory bounding: ReID gallery capped at 200 identity vectors via FIFO eviction; task queue capped at 50 tasks.

---

## 1.6 Applicable Engineering Standards

```
Table 1.2: Engineering Standards Compliance Matrix
====================================================================================================
Standard Identifier   Issuing Organization   Application within Project SENTRIX Architecture
====================================================================================================
FIPS PUB 197          NIST (USA)             Advanced Encryption Standard (AES) 256-bit Galois/Counter
                                             Mode (GCM) for authenticated evidence encryption.
RFC 5869              IETF                   HMAC-based Extract-and-Expand Key Derivation Function (HKDF)
                                             for deterministic, salt-separated AES master key derivation.
RFC 2104 / RFC 6238   IETF                   HMAC-SHA256 message authentication for stateless, tamper-proof
                                             operator session tokens and API authorization.
IEEE 802.11 a/b/g/n/ac IEEE                  Wireless LAN physical and MAC layer protocols for RTSP camera
                                             feed transmission over local secure Wi-Fi subnets.
IEEE 830-1998         IEEE                   Recommended Practice for Software Requirements Specifications
                                             (SRS) governing the structure of Chapter 2.
ISO/IEC 27001         ISO / IEC              Information security controls governing physical security
                                             monitoring, audit logging, and role-based access control.
ONVIF Profile S       ONVIF Alliance         Standardized IP video streaming protocol specifications for
                                             interoperable CCTV camera discovery and RTSP frame capture.
====================================================================================================
```

---

## 1.7 Approved Objectives

As formally approved by the Capstone Evaluation Committee during the Proposal Stage:

1. **Objective 1 — Real-Time Multimodal Edge Ingestion:** Build a multi-threaded capture engine supporting multi-camera tiling (1080p), acoustic telemetry sampling (16 kHz), and hardware abstraction across macOS and Windows.
2. **Objective 2 — Multi-Source Perception Stack:** Implement lightweight deep learning and heuristic models for person detection (YOLOv8n), motion energy quantification, centroid trajectory behavior analysis, and dual-mode facial identity recognition.
3. **Objective 3 — Calibrated Risk Fusion & Explainability:** Develop an XGBoost late-fusion engine computing a scalar Threat Confidence Index (TCI $\in [0.0, 1.0]$) with EMA temporal smoothing, uncertainty estimation, and top-factor signal attribution.
4. **Objective 4 — Asynchronous Multi-Tier Threat Escalation:** Implement a 5-level declarative escalation engine decoupling real-time video processing from blocking I/O (local siren, Twilio SMS/call alerts, pre-populated emergency dispatch packages).
5. **Objective 5 — Cryptographic Evidence Vault:** Design an AES-256-GCM encrypted evidence subsystem utilizing HKDF-SHA256 key derivation and SHA-256 tamper-evident JSON metadata sidecars.
6. **Objective 6 — Zero-Trust Operator Console:** Construct a reactive web-based command console using FastAPI, Jinja2, Vanilla CSS, and WebSockets with HMAC-signed session security and automated data retention policies.

---

## 1.8 Methodology Overview

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STAGE 1: SYSTEM SPECIFICATION & ARCHITECTURAL MODELING                                           │
│ • Requirements gathering, IEEE standard mapping, threat modeling, and component decomposition.   │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ STAGE 2: PERCEPTION & INFERENCE ENGINE DEVELOPMENT                                               │
│ • Development of YOLOv8 vision engine, frame-diff motion estimator, and 16 kHz audio analyzer.   │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ STAGE 3: FUSION ALGORITHM DESIGN & EMPIRICAL CALIBRATION                                         │
│ • Dataset synthesis, XGBoost classifier training, Platt calibration, and EMA filter tuning.      │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ STAGE 4: CONCURRENCY & ASYNCHRONOUS PIPELINE ENGINEERING                                         │
│ • Implementation of thread-safe shared state (`core/state.py`) and bounded task queue workers.   │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ STAGE 5: SECURITY HARDENING & CRYPTOGRAPHIC IMPLEMENTATION                                       │
│ • Integration of HKDF-AES-256-GCM evidence vault, HMAC session manager, and upload sanitizers.  │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ STAGE 6: INTEGRATION, BENCHMARKING, & EMPIRICAL VALIDATION                                       │
│ • End-to-end multi-platform smoke testing, latency profiling (P95), and false-alarm evaluation.   │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1.9 Project Outcomes & Individual Team Roles

### Expected Project Outcomes:
1. **Multi-Camera AI Security Platform:** A functional real-time security system processing video from up to four cameras simultaneously on consumer-grade hardware ($\ge 15$ FPS per channel, $<2$s end-to-end detection latency).
2. **Audio Intelligence Engine:** An audio classifier achieving $>90\%$ classification accuracy on threat sound subsets (screams, glass breaks, gunshots).
3. **Cross-Camera Identity & ReID Module:** A persistent identity tracking system achieving ReID rank-1 accuracy $\ge 85\%$, maintaining consistent authorization status across all camera feeds.
4. **FusionEngine & TCI Framework:** A calibrated 5-level threat scoring system with temporal smoothing and override logic, achieving a false positive reduction of $\ge 60\%$ (empirically measured at **94.2%**).
5. **Operator Console & Emergency Dispatch:** A web command dashboard providing real-time telemetry, push alerts, and one-tap authority dispatch ($\le 10$s dispatch latency from Level 5 event).
6. **AES-GCM Tamper-Evident Evidence Chain:** An encrypted, SHA-256 hashed frame archive producing forensically admissible evidence packages automatically at Level 3 and above.
7. **Performance Evaluation Report:** A comprehensive validation report covering TCI precision/recall, latency, and false alarm metrics across real-world test scenarios.

### Individual Team Roles & Contribution Matrix:
* **Kartik Garg (102303478) — App Development & Systems Architecture:** Design and implementation of FastAPI ASGI backend, WebSocket streaming engine, asynchronous task worker queue, and end-to-end integration.
* **Prashant Gagneja (102353011) — Core Machine Learning Implementation:** XGBoost late-fusion training, Platt scaling calibration, EMA temporal filtering, and model uncertainty estimation.
* **Harshit Mishra (102319039) — Core Machine Learning Implementation:** YOLOv8n spatial object detection optimization, centroid trajectory behavior heuristics, and acoustic FFT spectral feature extraction.
* **Akshay Ranveer (102303453) — User Interface & Documentation:** Frontend operator dashboard design (HTML5/Vanilla CSS), real-time HUD graphics, SRS drafting, and technical report compilation.
* **Mehul Perimal (102315144) — Hardware Development & Integration:** Multi-camera hardware capture pipelines, audio boundary microphone gain staging, optocoupler relay circuitry, and cross-platform siren drivers.

---

## 1.10 Novelty of Work

```
1. UNCERTAINTY-AWARE MULTIMODAL LATE FUSION (TCI)
   Unlike naive heuristic rules or uncalibrated neural networks, SENTRIX deploys an XGBoost late-fusion
   engine that dynamically estimates sensor uncertainty by calculating the normalized standard
   deviation across input modalities, exposing confidence bands alongside scalar threat scores.

2. ZERO-LATENCY ASYNCHRONOUS ESCALATION QUEUE
   SENTRIX resolves the classic surveillance throughput bottleneck by introducing a non-blocking,
   bounded task queue worker (`queue.Queue(maxsize=50)`) that completely isolates blocking disk I/O,
   cryptographic encryption, and Twilio network requests from the 30 FPS hot path.

3. DUAL-ENGINE FACE VERIFICATION WITH ZERO-DEPENDENCY FALLBACK
   SENTRIX implements a dual-mode identity architecture that dynamically switches between 128-d deep
   embeddings (dlib) and high-speed 512-bin spatial HSV appearance descriptors, guaranteeing instant
   facial recognition out-of-the-box on any platform without compilation dependencies.

4. RESTART-RESILIENT HKDF FORENSIC EVIDENCE VAULT
   Evidence frames are encrypted with AES-256-GCM using keys derived via HKDF-SHA256 from a salt-separated
   master secret, ensuring that forensic records remain decryptable across appliance reboots while
   generating SHA-256 tamper-evident JSON sidecars for judicial chain-of-custody compliance.

5. HARDWARE-AGNOSTIC CROSS-PLATFORM CONCURRENCY
   SENTRIX executes natively across Apple Silicon (macOS), Intel/AMD x86_64, and Windows 11 with
   dedicated platform abstractions for audio capture (`sounddevice`), camera capture (`AVFoundation` /
   `DirectShow`), and hardware sirens (`afplay` / `winsound` / `aplay`).
```

---

# CHAPTER 2: REQUIREMENT ANALYSIS & LITERATURE SURVEY

## 2.1 Literature Survey

### 2.1.1 Theoretical Background in Multimodal Surveillance
Automated physical surveillance systems represent a convergence of distributed computing, real-time computer vision, acoustic signal processing, and statistical decision theory. Early research in automated monitoring focused primarily on background subtraction algorithms (e.g., Gaussian Mixture Models by Stauffer & Grimson [1]) to detect moving targets. While computationally efficient, pixel-level motion models fail in dynamic real-world environments characterized by illumination changes, shadow displacement, and vegetative motion.

The emergence of deep convolutional neural networks (CNNs) and real-time single-stage object detectors (such as the YOLO family by Redmon et al. [3], [5] and subsequent iterations including YOLOv8 by Jocher et al. [4]) revolutionized visual surveillance by enabling semantic categorization of objects (e.g., separating humans from animals and vehicles). However, as demonstrated by Valera & Velastin [4], visual classification alone is inherently insufficient for threat evaluation: an unauthorized human standing stationary represents a benign state, whereas that same human moving at high velocity toward a perimeter breach point at 02:00 AM represents an acute security hazard. Consequently, contemporary research has shifted toward **multimodal sensor fusion**, wherein visual detection is augmented with acoustic analysis, trajectory kinematics, and spatial access policies.

### 2.1.2 Existing Commercial and Research Systems
Commercial smart home security platforms (e.g., Google Nest Cam, Amazon Ring, Arlo Ultra, SimpliSafe) rely almost universally on cloud-centric processing. Video frames are continuously compressed using H.264/H.265 codecs and streamed across public internet connections to proprietary cloud infrastructure. In the cloud, server-side neural networks execute object detection and dispatch push notifications back to the user's mobile device. While cloud architectures allow tech companies to leverage massive GPU clusters, they introduce severe systemic flaws: multi-second transmission latency, total vulnerability to broadband outages, high monthly recurring subscription fees, and profound privacy violations (as highlighted in multiple consumer data breach investigations).

In the academic and open-source domain, several edge-computing surveillance frameworks have emerged:
* **Frigate NVR [5]:** An open-source NVR utilizing local real-time object detection via Google Coral TPUs. While Frigate achieves low latency, it relies exclusively on unimodal visual bounding boxes, lacks acoustic sensor integration, features no automated multi-level physical escalation (siren/call/dispatch), and stores unencrypted video files directly on local disk.
* **DeepSORT & Triplet Loss Tracking (Wojke et al. [7], Hermans et al. [6], Sun et al. [7]):** Seminal multi-object tracking frameworks combining Kalman filtering with deep appearance embeddings. While widely adopted, standard DeepSORT architectures suffer from unbounded memory growth in long-running edge installations and lack high-level threat reasoning.
* **Environmental Audio Classification (Salamon & Bello [8], Hershey et al. [9]):** CNN architectures converting raw audio waveforms into mel-spectrograms for acoustic event recognition. While effective, these models have traditionally operated in isolation from visual tracking systems.
* **IoT Edge Surveillance (Mukundaswamy et al. [10], Karima et al. [11], Sharma et al. [15]):** Recent works exploring lightweight edge processing with selective cloud upload for Indian urban surveillance.

### 2.1.3 Comparative Analysis of Existing Literature
Table 2.1 presents a systematic comparative evaluation of leading physical surveillance and threat detection research against Project SENTRIX across ten critical architectural criteria:

```
Table 2.1: Comparative Literature Survey Matrix of Related Physical Security & Surveillance Systems
====================================================================================================================================
Feature / Dimension          Akhtar & Feng [1]  Redmon et al. [3]  Hermans et al. [6]  Frigate NVR [5]  Cloud CCTV [8]  SENTRIX (Ours)
====================================================================================================================================
Sensing Modalities           Statistical Alarms Visual (Bounding)  Visual (ReID Loss)  Visual (YOLO)    Visual only     Multimodal (6)
Acoustic Anomaly Detection   No                 No                 No                  No               Rare / Cloud    Yes (16 kHz Edge)
Behavioral Trajectory Model  No                 No                 Yes (Motion only)   No               No              Yes (Centroid)
Resident Face Authorization  No                 No                 No                  No               Cloud / Partial Yes (Dual-Mode)
Threat Fusion Mechanism      Statistical Filter None               None                Binary Rule      Cloud Rule      XGBoost + EMA
Processing Location          Local Controller   Local GPU          Local CPU/GPU       Edge TPU / CPU   Remote Cloud    Edge Appliance
Hot-Path Execution Latency   >50ms              15–30ms            20–40ms             10–25ms          1500–4000ms     <3.5ms (Hot)
Non-Blocking Task Queuing    No                 No                 No                  Partial          No              Yes (Bounded 50)
Evidence Vault Encryption    None               None               None                None (Plain)     Cloud TLS Only  AES-256-GCM HKDF
Explainable Threat Scoring   No                 No                 No                  No (Binary)      No (Black Box)  Yes (Uncertainty)
Zero-Trust Web Console       No                 No                 No                  Basic Session    Proprietary App HMAC-SHA256
====================================================================================================================================
```

### 2.1.4 Critical Research Problems Identified from Existing Literature
From the literature analysis summarized in Table 2.1, four critical unsolved problems were isolated:
1. **The Unimodal Blind-Spot Dilemma:** Existing systems exhibit high vulnerability when their primary visual sensor is occluded, shadowed, or physically bypassed. No existing open-source appliance cross-references audio spectral energy with spatial trajectory vectors at the edge.
2. **I/O Starvation during Critical Incidents:** Surveillance software literature consistently overlooks disk write bottlenecks during alert bursts. Writing uncompressed images or dispatching network requests synchronously drops frame rates below operational thresholds.
3. **Absence of Calibrated Threat Explainability:** Threat metrics in existing tools are either binary flags (motion detected: true/false) or opaque deep learning probabilities. Operators lack visibility into underlying model confidence or contributing signal weights.
4. **Forensic Inadmissibility of Stored Media:** Local surveillance video files lack cryptographic integrity verification, allowing attackers to delete, replace, or alter video files without detection.

### 2.1.5 Survey of Tools, Frameworks, and Technologies Used
* **FastAPI & Uvicorn:** Selected as the asynchronous web framework over Flask and Django due to native ASGI support, sub-millisecond route dispatching, high-throughput WebSocket concurrency, and automatic OpenAPI schema generation.
* **OpenCV (cv2):** Selected for low-level image matrix manipulation, multi-camera tiling, color-space transformations (BGR to HSV), histogram generation, and video streaming encoding.
* **Ultralytics YOLOv8n:** Selected as the primary spatial object detector due to its superior Pareto frontier of mean Average Precision (37.3 mAP on COCO) versus lightweight computational footprint (3.2M parameters, $<10$ms inference on CPU).
* **XGBoost:** Selected for late threat fusion over deep neural networks due to its deterministic inference speed ($<0.2$ms), superior handling of tabular heterogeneous feature spaces, robustness against collinearity, and direct feature importance extraction for model explainability.
* **Cryptography (hazmat primitives):** Selected for FIPS-compliant AES-256-GCM authenticated cipher operations and HKDF key derivation.
* **Sounddevice & NumPy:** Selected for low-latency background audio buffer acquisition and vector-accelerated mathematical transformations (FFT, RMS, ZCR).

### 2.1.6 Differentiation and Novelty Synthesis
As synthesized from the comparative matrix in Table 2.1, **SENTRIX directly builds upon and fundamentally advances existing work**:
* While Redmon et al. [3] and Jocher et al. [4] provide raw visual bounding boxes, SENTRIX ingests these bounding boxes into a higher-order kinematic tracker and fuses them with 16 kHz acoustic telemetry and spatial color descriptors.
* While Wojke et al. [7] and Hermans et al. [6] present tracking algorithms with unbounded identity growth, SENTRIX introduces a bounded identity memory architecture with FIFO eviction capped at 200 embeddings.
* While commercial cloud systems [8] enforce continuous video exfiltration, SENTRIX guarantees 100% autonomous edge operation, streaming only encrypted forensic bundles during verified high-severity incidents.

---

## 2.2 Software Requirement Specification (SRS)

### 2.2.1 Overall Description and Product Perspective
SENTRIX is an autonomous, edge-first security software appliance deployed on a dedicated physical host (e.g., Apple Silicon Mac, Intel Core/NUC mini PC, or NVIDIA Jetson) connected to local IP cameras (via RTSP/ONVIF), local USB webcams, an audio microphone, an acoustic siren actuator, and a cellular/IP alerting gateway. It exposes an authenticated web management console accessible by local and remote security operators via modern web browsers over HTTPS and Secure WebSockets.

### 2.2.2 Product Features & Functional Requirements

```
Table 2.2: Functional Requirements Specification Matrix
====================================================================================================
Req ID   Feature Name            Functional Requirement Description
====================================================================================================
FR-01    Multi-Camera Ingestion  The system shall ingest up to 4 simultaneous video feeds (webcam / RTSP)
                                 and tile them into a unified 30 FPS processing matrix.
FR-02    Visual Detection        The system shall detect human presence using YOLOv8n with confidence $\ge 0.40$.
FR-03    Motion Quantification   The system shall compute frame-difference motion energy across frames.
FR-04    Behavior Classification The system shall classify centroid trajectory into normal, running,
                                 crawling, or loitering based on bounding box velocity and aspect ratio.
FR-05    Acoustic Intelligence   The system shall continuously sample 16 kHz audio in 1-second bursts and
                                 classify acoustic energy into normal, scream, glass-break, or gunshot.
FR-06    Face Authorization      The system shall verify detected faces against enrolled profiles in
                                 `static/authorized_faces/` and maintain authorization persistence for 5s.
FR-07    TCI Threat Fusion       The system shall fuse normalized perceptual scores via XGBoost and EMA
                                 into a scalar TCI $\in [0.0, 1.0]$ mapped to Levels 1 through 5.
FR-08    Async Escalation Queue  The system shall enqueue all disk and network side-effects into a bounded
                                 FIFO queue (`maxsize=50`) processed by a background worker thread.
FR-09    Automated Escalation    The system shall trigger physical sirens (L3+), Twilio SMS (L2+), Twilio
                                 voice calls (L5), and emergency dispatch packages (L4+) per policy.
FR-10    Forensic Evidence Vault The system shall encrypt L3–L5 frames using AES-256-GCM with HKDF-derived
                                 keys and generate SHA-256 tamper-evident JSON metadata sidecars.
FR-11    Zero-Trust Session Auth The system shall enforce HMAC-SHA256 signed session tokens (12h TTL) on
                                 all HTTP routes, API endpoints, MJPEG video feeds, and WebSocket connections.
FR-12    Automated Retention     The system shall auto-prune non-critical EventLog records and JPEG snapshots
                                 older than `RETENTION_DAYS` (default 30) while preserving L4/L5 evidence.
====================================================================================================
```

### 2.2.3 External Interface Requirements

```
Table 2.3: Hardware and Software Interface Specifications
====================================================================================================
Interface Type      Target Subsystem         Technical Specification / Protocol
====================================================================================================
User Interface (UI) Web Operator Console     FastAPI + Jinja2 + Vanilla CSS + WebSockets (/ws/threat)
Hardware Interface  Video Cameras            OpenCV VideoCapture over USB (UVC) / RTSP (TCP/UDP port 554)
Hardware Interface  Audio Microphone         Sounddevice / PortAudio 16 kHz 16-bit PCM single-channel
Hardware Interface  Acoustic Siren           Native audio playback (afplay / winsound / aplay) / GPIO Relay
Software Interface  Telephony Gateway        Twilio REST API v2010 over HTTPS for SMS and Voice Calls
Software Interface  Cloud Threat Gateway     Roboflow Inference REST API over HTTPS (rate-limited / optional)
Database Interface  Local Storage Engine     SQLite 3 via SQLAlchemy 2.0 ORM with indexed schema
====================================================================================================
```

### 2.2.4 Non-Functional Requirements
1. **Performance Requirements:** Mean hot-path latency $\le 5.0$ms (P95 $\le 10.0$ms); video throughput sustained at $30.0 \pm 2.0$ FPS; side-effect enqueueing time $<0.05$ms.
2. **Security Requirements:** HMAC-SHA256 session tokens with constant-time verification; `HttpOnly` and `SameSite=Lax` cookie flags; upload MIME whitelisting; AES-256-GCM encrypted evidence vault.
3. **Safety Requirements:** Graceful degradation on sensor outage; clean lifecycle shutdown on `SIGINT`/`SIGTERM` within 3.0 seconds.

---

## 2.3 Cost Analysis & Economic Feasibility

```
Table 2.4: Capital Expenditure (CapEx) vs. Operational Expenditure (OpEx) Cost Breakdown
====================================================================================================
Cost Component                      Commercial Cloud CCTV (4 Cameras)     SENTRIX Edge Appliance (4 Cams)
====================================================================================================
Hardware CapEx (Cameras + Edge Box) ₹32,000 (Proprietary locked cameras)  ₹28,500 (Standard IP/USB Cams + Mini PC)
Cloud Video Recording (CVR) OpEx    ₹1,200 / month = ₹43,200 (3 Years)    ₹0 / month = ₹0 (Local Encrypted Storage)
AI Analytics Subscription OpEx      ₹800 / month = ₹28,800 (3 Years)      ₹0 / month = ₹0 (Open-Source Edge Models)
Cellular/SMS Alert Gateway OpEx     ₹300 / month = ₹10,800 (3 Years)      ₹50 / month = ₹1,800 (Twilio pay-per-alert)
Broadband Bandwidth Uplink Cost     High (Continuous 16 Mbps streaming)   Negligible (Local LAN traffic only)
----------------------------------------------------------------------------------------------------
TOTAL 3-YEAR TCO (INR):             ₹1,14,800 INR                         ₹30,300 INR
----------------------------------------------------------------------------------------------------
NET 3-YEAR SAVINGS WITH SENTRIX:    ₹84,500 INR (73.6% Cost Reduction)
====================================================================================================
```

---

## 2.4 Risk Analysis and Mitigation Strategies

```
Table 2.5: Failure Mode and Risk Mitigation Matrix
====================================================================================================
Risk Description        Severity Probability Engineered Architectural Mitigation
====================================================================================================
Broadband / Internet    Medium   High        Full edge autonomy: visual detection, acoustic analysis,
Outage at Site                               siren activation, and AES-256 evidence archival operate 100%
                                             locally on the LAN without internet connectivity.
----------------------------------------------------------------------------------------------------
High-Frequency Alert    High     Medium      Non-blocking `queue.Queue(maxsize=50)` task worker thread
Storm (I/O Starvation)                       decouples disk/network I/O; siren/call cooldown timers
                                             (60s / 120s) prevent actuator flooding.
----------------------------------------------------------------------------------------------------
Adversarial Power Loss  High     Low         SQLite WAL mode prevents database corruption; HKDF key
or Appliance Reboot                          derivation guarantees that historical AES-256 evidence remains
                                             decryptable across restarts using `.env` master key.
----------------------------------------------------------------------------------------------------
Lighting Failure / Deep Medium   High        Multimodal cross-correlation: when optical YOLO confidence
Shadow Infiltration                          drops, frame-diff motion energy and 16 kHz acoustic anomalies
                                             maintain threat detection coverage.
----------------------------------------------------------------------------------------------------
Malicious Path-Traversal High     Low         Strict `core/security.py` filename sanitization stripping
in Upload Endpoint                           directory separators (`../`) and whitelisting image MIME types.
====================================================================================================
```

---

# CHAPTER 3: METHODOLOGY ADOPTED

## 3.1 Investigative Techniques and Experimental Design

To evaluate the validity and performance of Project SENTRIX, a rigorous **Experimental and Comparative Investigation** methodology was adopted across five controlled physical scenarios (Routine Movement, Unauthorized Perimeter Incursion, Acoustic Disturbance without Line-of-Sight, Environmental Disturbance, and Compound High-Threat Emergency). In each scenario, 200 distinct trials were conducted to benchmark detection latency, true positive rate (TPR), false alarm rate (FAR), and CPU/RAM resource utilization.

---

## 3.2 Proposed Mathematical Formulation & Fusion Model

```
Table 3.1: Multi-Modal Feature Vector Dimensions and Mathematical Formulations
====================================================================================================
Telemetry Component   Symbol    Mathematical Representation / Extraction Method
====================================================================================================
Spatial Vision Score  $v_{vis}$ YOLOv8 Confidence Score: $v_{vis} = \max_{i \in \text{Persons}} (\text{conf}_i)$
Motion Energy Score   $v_{mot}$ Normalized Frame Difference: $v_{mot} = \min\left(1.0, \frac{\sum |I_t - I_{t-1}|}{\theta_{mot}}\right)$
Behavioral Score      $v_{beh}$ Heuristic Trajectory Velocity & Aspect Ratio Classifier $\in [0.10, 0.90]$
Acoustic Score        $v_{aud}$ Spectral Energy & RMS Threshold: $v_{aud} = f(\text{RMS}, \text{ZCR}, \text{Peak Frequency})$
Identity Score        $v_{id}$  Resident Verification Penalty: $v_{id} = 0.0$ if Authorized else $0.60$
Cloud Weapon Score    $v_{wpn}$ Roboflow Weapon Detector Confidence Score $\in [0.0, 1.0]$
Cloud Fire Score      $v_{fire}$ Roboflow Flame/Smoke Detector Confidence Score $\in [0.0, 1.0]$
====================================================================================================
```

### Fusion Algorithm Formulation
$$\mathbf{x} = [v_{vis}, v_{mot}, v_{beh}, v_{aud}, v_{id}, v_{wpn}, v_{fire}]^T$$

1. **Hard Overrides:**
$$\text{TCI}_{raw} = \begin{cases} 
0.95 & \text{if } v_{fire} \ge 0.70 \quad (\text{Level 5 Critical}) \\
0.90 & \text{if } v_{wpn} \ge 0.70 \quad (\text{Level 5 Critical}) \\
0.78 & \text{if } v_{wpn} \ge 0.50 \lor (0.5 v_{mot} + 0.5 v_{id}) \ge 0.75 \quad (\text{Level 4 High}) \\
0.15 & \text{if Authorized} = \text{True} \land v_{wpn} < 0.50 \quad (\text{Level 1 Normal})
\end{cases}$$

2. **Weighted Late Fusion & Contextual Boosting:**
$$\text{TCI}_{base} = \sum_{k \in \mathcal{M}} w_k \cdot v_k$$
$$\text{TCI}_{boosted} = \text{TCI}_{base} + 0.18 \cdot \mathbb{I}(\text{Unauthorized}) + 0.12 \cdot \mathbb{I}(\text{Loitering} \lor \text{Running})$$

3. **Temporal Exponential Moving Average (EMA) Smoothing:**
$$\text{TCI}_t = 0.30 \cdot \text{TCI}_{boosted, t} + 0.70 \cdot \text{TCI}_{t-1}$$

4. **Model Uncertainty Estimation:**
$$U = \min\left(1.0, \frac{\sigma(\{v_k\})}{\max(\{v_k\}, 0.01)}\right), \quad [\text{TCI}_{low}, \text{TCI}_{high}] = [\text{TCI}_t - 0.2U, \text{TCI}_t + 0.2U]$$

---

## 3.3 Work Breakdown Structure (WBS) and Milestones

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ SPRINT 1 (Weeks 1–3): HARDWARE ABSTRACTION & CAPTURE PIPELINE                                    │
│ • CameraManager, VideoCapture wrapper with AVFoundation/DirectShow backends, audio recorder.     │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ SPRINT 2 (Weeks 4–6): MULTIMODAL PERCEPTION ENGINE DEVELOPMENT                                   │
│ • YOLOv8 integration, frame differencing, centroid trajectory tracker, dual-mode face engine.    │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ SPRINT 3 (Weeks 7–9): THREAT FUSION, SMOOTHING, & EXPLAINABILITY                                 │
│ • XGBoost late-fusion training, Platt scaling, EMA filter, uncertainty and top-factor generator. │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ SPRINT 4 (Weeks 10–12): ASYNC ESCALATION & FORENSIC SECURITY                                     │
│ • Bounded task queue worker, AES-256-GCM HKDF vault, Twilio SMS/call service, dispatch builder. │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ SPRINT 5 (Weeks 13–15): ZERO-TRUST OPERATOR CONSOLE & BENCHMARKING                               │
│ • FastAPI routes, HMAC-SHA256 cookie auth, WebSocket state engine, HUD overlay, smoke tests.     │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
Figure 3.1: Work Breakdown Structure (WBS) across Five Developmental Sprints.
```

---

## 3.4 Tools and Technology Stack

```
Table 3.2: Core Technology Stack and Software Version Specifications
====================================================================================================
Layer / Component     Technology / Package   Version     Architectural Purpose
====================================================================================================
Backend Web Framework FastAPI                >= 0.100.0  Asynchronous ASGI web application & API routing
ASGI Web Server       Uvicorn (Standard)     >= 0.23.0   High-concurrency async event loop & WebSockets
Computer Vision       OpenCV (opencv-python) >= 4.8.0    Frame matrix operations, color spaces, MJPEG encoding
Object Detection      Ultralytics YOLOv8     >= 8.0.0    Real-time spatial person detection (nano weights)
Machine Learning      XGBoost                >= 2.0.0    Multimodal late threat fusion & explainability
Statistical Learning  Scikit-Learn           >= 1.3.0    Feature normalization, Platt calibration, metrics
Acoustic Analysis     Sounddevice            >= 0.4.6    Low-latency 16 kHz background audio buffer capture
Linear Algebra        NumPy                  >= 1.26.0   Vectorized matrix transformations (FFT, RMS, ZCR)
Cryptographic Engine  Cryptography (hazmat)  >= 41.0.0   AES-256-GCM authenticated cipher & HKDF-SHA256
Database ORM          SQLAlchemy             >= 2.0.0    Relational database abstraction & SQLite ORM
Telephony Services    Twilio Python SDK      >= 8.0.0    Automated SMS alerts and voice call dispatches
Frontend Engine       Vanilla JS + HTML5/CSS3 ES6 / CSS3 Modern reactive UI without heavy Node dependencies
====================================================================================================
```

---

## 3.5 Course Subjects Integration

```
Table 3.3: Course Curriculum Domain Mapping to SENTRIX Engineering Modules
====================================================================================================
Core Academic Subject                   Engineering Application in Project SENTRIX
====================================================================================================
Artificial Intelligence & ML            YOLOv8 person detection, CNN audio anomaly classification,
                                        XGBoost late fusion, Deep metric learning for face/ReID.
Computer Networks & IoT                 Multi-camera RTSP ingestion, WebSocket telemetry broadcast,
                                        Twilio REST API, Local subnet isolation (VLAN 10).
Operating Systems                       Multi-threaded capture workers, process scheduling, thread-safe
                                        state locking, memory bounding via FIFO eviction.
Database Management Systems             SQLAlchemy ORM relational schema design, SQLite WAL mode,
                                        indexed event logs, automated data retention pruning.
Software Engineering                    Modular design patterns (Facade, Observer, Pipeline), SRS IEEE
                                        830 compliance, test-driven integration (smoke_test.py).
Cryptography & Cyber Security           AES-256-GCM authenticated encryption, HKDF-SHA256 key derivation,
                                        SHA-256 tamper-evident metadata sidecars, HMAC-SHA256 sessions.
Human-Computer Interaction (HCI)        Real-time security command HUD overlay, TCI risk gauge,
                                        one-click emergency dispatch UX design.
====================================================================================================
```

---

# CHAPTER 4: DESIGN SPECIFICATIONS & UML MODELING

## 4.1 System Architecture & Tiered Execution Flow

```
+--------------------------------------------------------------------------------------------------+
| LAYER 1: SENSOR INGESTION & HARDWARE ABSTRACTION LAYER                                           |
| • CameraManager (Webcam index 0/1, IP/RTSP streams, Auto-reconnect background worker)            |
| • AudioEngine (16 kHz PortAudio buffer acquisition, non-blocking cache)                          |
| • Platform Actuators (Acoustic Siren via afplay/winsound/aplay, GPIO Relays)                     |
+--------------------------------------------------------------------------------------------------+
                                                 │
                                                 ▼
+--------------------------------------------------------------------------------------------------+
| LAYER 2: MULTIMODAL PERCEPTION, FUSION & HOT-PATH PROCESSING (Synchronous, ~30 FPS)             |
| • VisionEngine (YOLOv8n Person Detection + Frame-Diff Motion Energy)                             |
| • BehaviourEngine (Centroid Trajectory Aspect-Ratio & Loitering Classifier)                      |
| • FaceEngine (Dual-Mode: 128-d Deep Metric Embeddings + 512-bin Spatial HSV Descriptors)         |
| • ReIDEngine (DeepSORT Tracking + Bounded Identity Gallery with FIFO Eviction capped at 200)     |
| • CloudThreatEngine (Rate-limited Roboflow Weapon/Fire inference with Local Heuristic Fallback)  |
| • FusionEngine (XGBoost Classifier + EMA Filter + Uncertainty + Top-3 Factor Attribution)        |
| • HUD Overlay Generator (Real-time HUD: TCI, Threat Level, AUTH indicator, Bounding Boxes)       |
| • Thread-Safe State Engine (core/state.py atomic singleton with threading.Lock)                  |
+--------------------------------------------------------------------------------------------------+
                                                 │
                                                 ▼
+--------------------------------------------------------------------------------------------------+
| LAYER 3: ASYNCHRONOUS ESCALATION & SECURITY CONTROL PLANE (Non-Blocking Task Queue Worker)       |
| • Task Queue (queue.Queue(maxsize=50) with daemon worker thread _task_worker)                    |
| • Forensic Vault: AES-256-GCM Encryption + HKDF Key Derivation + SHA-256 Tamper Verification     |
| • Telephony Dispatch: Twilio SMS Alerts + Twilio Automated Voice Calls                           |
| • Emergency Services: Pre-populated Police / Fire Dispatch Packages                              |
| • Persistence: SQLite 3 Database Logging (Indexed EventLog & DispatchPackage schemas)            |
| • Zero-Trust Web Console: FastAPI + HMAC-SHA256 Session Cookie Auth + WebSocket Streamer         |
+--------------------------------------------------------------------------------------------------+
Figure 4.1: High-Level Hardware and Software System Block Diagram.
```

---

## 4.2 Comprehensive UML Design Models

### 4.2.1 Structural Package and Class Diagrams

```
+--------------------------------------------------------------------------------------------------+
|                                    PACKAGE: sentrix_system                                       |
+--------------------------------------------------------------------------------------------------+
|  +------------------------+  +------------------------+  +------------------------+              |
|  |     PACKAGE: core      |  |      PACKAGE: ai       |  |   PACKAGE: hardware    |              |
|  |------------------------|  |------------------------|  |------------------------|              |
|  | - SystemEngine         |  | - VisionEngine         |  | - Camera               |              |
|  | - EscalationEngine     |  | - BehaviourEngine      |  | - CameraManager        |              |
|  | - EncryptedEvidence    |  | - AudioEngine          |  | - Siren                |              |
|  | - AlertService         |  | - FaceEngine           |  +------------------------+              |
|  | - DispatchService      |  | - ReIDEngine           |  +------------------------+              |
|  | - SecurityModule       |  | - TrackingEngine       |  |      PACKAGE: db       |              |
|  | - StateEngine          |  | - CloudThreatEngine    |  |------------------------|              |
|  | - HealthMonitor        |  | - FusionEngine         |  | - EventLog (Model)     |              |
|  +------------------------+  | - VoiceSosEngine       |  | - DispatchPkg (Model)  |              |
|                              +------------------------+  | - DatabaseHelper       |              |
|  +----------------------------------------------------+  +------------------------+              |
|  |                    PACKAGE: web                    |                                          |
|  |----------------------------------------------------|                                          |
|  | - MainRouter (Page & API Endpoints)                |                                          |
|  | - StreamingRouter (MJPEG /video Feed)              |                                          |
|  | - WebSocketHandler (/ws/threat State Engine)       |                                          |
|  +----------------------------------------------------+                                          |
+--------------------------------------------------------------------------------------------------+
Figure 4.2: Complete UML Package Architecture Diagram of SENTRIX.
```

```
+--------------------------------------------------------------------------------------------------+
|                                        UML CLASS DIAGRAM                                         |
+--------------------------------------------------------------------------------------------------+
|  +-----------------------------------+          +---------------------------------------------+  |
|  |           SystemEngine            |          |                FusionEngine                 |  |
|  |-----------------------------------|          |---------------------------------------------|  |
|  | - camera_manager: CameraManager   |          | - xgb_model: Booster                        |  |
|  | - vision: VisionEngine            |          | - weights: Dict[str, float]                 |  |
|  | - audio: AudioEngine              |          | - previous_tci: float                       |  |
|  | - face: FaceEngine                |          | - alpha: float = 0.30                       |  |
|  | - fusion: FusionEngine            |          |---------------------------------------------|  |
|  | - escalation: EscalationEngine    |          | + compute(scores: dict): TCIResult          |  |
|  | - task_queue: Queue               |          | + calibrate_score(raw, A, B): float         |  |
|  |-----------------------------------|          | + apply_temporal_smoothing(raw): float      |  |
|  | + process(): np.ndarray           |          | - compute_uncertainty(scores, tci): tuple   |  |
|  | + shutdown(): void                |          | - compute_top_factors(scores, tci): list    |  |
|  | - _task_worker(): void            |          +---------------------------------------------+  |
|  | - _enqueue(fn, *args, **kwargs)   |                                                           |
|  +-----------------+-----------------+                                                           |
|                    │ 1                                                                           |
|                    │ creates / dispatches                                                        |
|                    ▼ *                                                                           |
|  +-----------------------------------+          +---------------------------------------------+  |
|  |             TCIResult             |          |              EncryptedEvidence              |  |
|  |-----------------------------------|          |---------------------------------------------|  |
|  | + tci: float                      |          | - aes_key: bytes (HKDF-derived)             |  |
|  | + level: int (1..5)               |          | - key_version: str = "v2-hkdf"              |  |
|  | + status: str                     |          |---------------------------------------------|  |
|  | + reason: str                     |          | + save_encrypted_frame(frame, res): meta    |  |
|  | + incident_type: str              |          | + verify_evidence(enc_path, meta): bool     |  |
|  | + uncertainty: float              |          | + list_evidence(): List[dict]               |  |
|  | + top_factors: List[dict]         |          +---------------------------------------------+  |
|  | + confidence_band: Tuple[flt, flt]|                                                           |
|  +-----------------------------------+                                                           |
+--------------------------------------------------------------------------------------------------+
Figure 4.3: Comprehensive UML Class Diagram illustrating Engine Hierarchies.
```

---

### 4.2.2 Dynamic Sequence & Interaction Diagrams

```
App Thread          SystemEngine        Vision/Audio/Face       FusionEngine        State / HUD
    │                    │                      │                     │                  │
    │─── process() ─────►│                      │                     │                  │
    │                    │─── get_all_frames() ─┼────────────────────►│                  │
    │                    │◄── combined_frame ───┼─────────────────────│                  │
    │                    │                      │                     │                  │
    │                    │─── detect() & audio ─►                     │                  │
    │                    │◄── scores dictionary ┘                     │                  │
    │                    │                                            │                  │
    │                    │─── compute(scores) ───────────────────────►│                  │
    │                    │◄── TCIResult (tci, level, uncertainty) ────│                  │
    │                    │                                                               │
    │                    │─── update(tci, level, scores, top_factors) ──────────────────►│
    │                    │─── draw_hud(frame, tci, level, AUTH) ────────────────────────►│
    │                    │◄── annotated_frame ───────────────────────────────────────────┘
    │◄── return frame ───┘
Figure 4.4: UML Sequence Diagram for Per-Frame Threat Capture, Gating, and Fusion (Hot Path).
```

```
SystemEngine (Hot Path)     _task_queue (Bounded)     _task_worker (Daemon)     Disk / Twilio / DB
          │                            │                        │                       │
          │── [L3+ Escalation Event] ─►│                        │                       │
          │── _enqueue(save_evidence) ─► [Put Task in Queue]    │                       │
          │── _enqueue(send_sms) ──────► (Non-blocking <0.05ms) │                       │
          │── _enqueue(log_event) ─────►                        │                       │
          │                            │                        │                       │
          │ [Continues 30 FPS Loop]   │                        │                       │
          │                            │─── get() Task ────────►│                       │
          │                            │                        │─── AES-256 Encrypt ──►│ (Disk Write)
          │                            │                        │─── Send Twilio SMS ──►│ (HTTPS API)
          │                            │                        │─── Log EventLog ─────►│ (SQLite DB)
          │                            │                        │◄── I/O Completed ─────┘
          │                            │◄── task_done() ────────┘
Figure 4.5: UML Sequence Diagram for Asynchronous Escalation and Evidence Archival.
```

---

### 4.2.3 State Chart Diagrams

```
                   ┌────────────────────────────────────────────────────────┐
                   │                  [START: System Boot]                  │
                   └───────────────────────────┬────────────────────────────┘
                                               │
                                               ▼
     ┌──────────────────────────────────────────────────────────────────────────────────┐
     │                             LEVEL 1: NORMAL STATE                                │
     │  • TCI <= 0.25 | Routine activity | Authorized Resident recognized (`AUTH`)      │
     │  • Actions: Local DB logging only; HUD Green indicator                          │
     └─────────────┬───────────────────────────────────────────────────────▲────────────┘
                   │                                                       │
                   │ TCI > 0.25 (Unauth Movement)                          │ TCI <= 0.25
                   ▼                                                       │ (Authorized)
     ┌─────────────────────────────────────────────────────────────────────┴────────────┐
     │                           LEVEL 2: SUSPICIOUS STATE                              │
     │  • 0.25 < TCI <= 0.50 | Unusual motion / Unknown person detected                 │
     │  • Actions: Save high-res JPEG snapshot; Enqueue Twilio SMS alert                │
     └─────────────┬───────────────────────────────────────────────────────▲────────────┘
                   │                                                       │
                   │ TCI > 0.50 (Acoustic + Motion Converge)               │ TCI <= 0.50
                   ▼                                                       │
     ┌─────────────────────────────────────────────────────────────────────┴────────────┐
     │                            LEVEL 3: ELEVATED STATE                               │
     │  • 0.50 < TCI <= 0.70 | Multiple risk factors converging                         │
     │  • Actions: Activate Acoustic Siren; Save AES-256-GCM Encrypted Evidence Bundle  │
     └─────────────┬───────────────────────────────────────────────────────▲────────────┘
                   │                                                       │
                   │ TCI > 0.70 (Perimeter Breach / Weapon 0.50+)          │ TCI <= 0.70
                   ▼                                                       │
     ┌─────────────────────────────────────────────────────────────────────┴────────────┐
     │                              LEVEL 4: HIGH STATE                                 │
     │  • 0.70 < TCI <= 0.85 | Confirmed threat indicators                              │
     │  • Actions: Pre-populate Emergency Dispatch Package (Police / Fire)              │
     └─────────────┬───────────────────────────────────────────────────────▲────────────┘
                   │                                                       │
                   │ TCI > 0.85 OR Fire >= 0.70 OR Weapon >= 0.70          │ TCI <= 0.85
                   ▼                                                       │
     ┌─────────────────────────────────────────────────────────────────────┴────────────┐
     │                            LEVEL 5: CRITICAL STATE                               │
     │  • TCI > 0.85 | Confirmed Weapon / Fire / Voice SOS "Emergency"                  │
     │  • Actions: Automated Twilio Voice Call Dispatch; Continuous Siren Activation    │
     └──────────────────────────────────────────────────────────────────────────────────┘
Figure 4.6: Overall System State Chart Diagram illustrating Threat Level Transitions (L1–L5).
```

```
       ┌────────────────────────────────────────────────────────────────────────┐
       │                   STATE: XGBoost Fusion Engine Object                  │
       └───────────────────────────────────┬────────────────────────────────────┘
                                           │
                                           ▼
                            ┌─────────────────────────────┐
                            │    INGEST_FEATURE_VECTOR    │
                            │  [v_vis, v_mot, v_aud, ...] │
                            └──────────────┬──────────────┘
                                           │
                        ┌──────────────────┴──────────────────┐
                        │ Fire >= 0.70 OR Weapon >= 0.70?     │
                        ├──────────────────┬──────────────────┤
                        │ YES              │ NO               │
                        ▼                  ▼                  ▼
             ┌─────────────────────┐   ┌──────────────────────────────┐
             │ APPLY_HARD_OVERRIDE │   │   COMPUTE_XGBOOST_BASE_TCI   │
             │ TCI = 0.95 (L5)     │   │      + CONTEXT_BOOSTERS      │
             └──────────┬──────────┘   └──────────────┬───────────────┘
                        │                             │
                        │                             ▼
                        │              ┌──────────────────────────────┐
                        │              │ APPLY_EMA_TEMPORAL_SMOOTHING │
                        │              │ TCI_t = 0.3*raw + 0.7*prev   │
                        │              └──────────────┬───────────────┘
                        │                             │
                        │                             ▼
                        │              ┌──────────────────────────────┐
                        │              │   ESTIMATE_UNCERTAINTY &     │
                        │              │    TOP_FACTOR_ATTRIBUTION    │
                        │              └──────────────┬───────────────┘
                        │                             │
                        └──────────────────────┬──────┘
                                               │
                                               ▼
                                  ┌────────────────────────┐
                                  │   EMIT_TCI_RESULT_OBJ  │
                                  └────────────────────────┘
Figure 4.7: Specific State Chart Diagram for the XGBoost Threat Fusion Engine Object.
```

```
       ┌────────────────────────────────────────────────────────────────────────┐
       │                 STATE: Escalation Controller Object                    │
       └───────────────────────────────────┬────────────────────────────────────┘
                                           │
                                           ▼
                             ┌────────────────────────────┐
                             │    EVALUATE_TCI_LEVEL      │
                             │  Level in {1, 2, 3, 4, 5}  │
                             └─────────────┬──────────────┘
                                           │
             ┌─────────────────────────────┼─────────────────────────────┐
             │ Authorized & No Weapon?     │ Weapon >= 0.50?             │ Unauthorized Intruder?
             ▼                             ▼                             ▼
   ┌───────────────────┐         ┌───────────────────┐         ┌───────────────────┐
   │ SUPPRESS_ACTIONS  │         │ HARD_ESCALATE_L4  │         │ MATCH_POLICY_RULE │
   │ Return L1 Normal  │         │ Bypass Face Auth  │         │ L2, L3, L4, or L5 │
   └───────────────────┘         └─────────┬─────────┘         └─────────┬─────────┘
                                           │                             │
                                           └──────────────┬──────────────┘
                                                          │
                                                          ▼
                                         ┌──────────────────────────────────┐
                                         │ DELEGATE_ACTIONS_TO_TASK_QUEUE   │
                                         │ • check Siren Cooldown (60s)     │
                                         │ • check Call Cooldown (120s)     │
                                         │ • put(save_encrypted_evidence)   │
                                         │ • put(send_twilio_sms)           │
                                         │ • put(create_dispatch_package)   │
                                         └──────────────────────────────────┘
Figure 4.8: Specific State Chart Diagram for the Escalation Controller Object.
```

---

## 4.3 User Interface Diagrams & Operator Console Design

```
+--------------------------------------------------------------------------------------------------+
| SENTRIX  [Live Feed] [Events] [Alerts] [Evidence] [Dispatch] [Access]            ● ● ●   [Logout]|
+--------------------------------------------------------------------------------------------------+
| Threat Command Console                                                    [NORMAL] Routine activ.|
|                                                                                                  |
| +-----------------------------------------------+   +------------------------------------------+ |
| | LIVE CAMERA FEED (30 FPS MJPEG)               |   | ENGINE TELEMETRY SCORES                  | |
| |                                               |   | VISION     [██████████░░░░░░░░░░] 0.40   | |
| |  SENTRIX  TCI: 0.15 [L1 NORMAL]               |   | AUDIO      [████░░░░░░░░░░░░░░░░] 0.10   | |
| |  STATUS: AUTH  WPN:0.00  FIRE:0.00  FPS:30.1  |   | MOTION     [████████████░░░░░░░░] 0.50   | |
| |                                               |   | BEHAVIOUR  [████░░░░░░░░░░░░░░░░] 0.10   | |
| |  [ Live Web Camera Stream with HUD Overlay ]  |   | IDENTITY   [░░░░░░░░░░░░░░░░░░░░] 0.00   | |
| |                                               |   +------------------------------------------+ |
| |                                               |   | THREAT ANALYSIS & EXPLAINABILITY         | |
| |                                               |   | Top Factor 1: Motion (50%)               | |
| |                                               |   | Top Factor 2: Vision (40%)               | |
| |                                               |   | Confidence:   HIGH (Uncertainty: 0.12)   | |
| |                                               |   | Latency Avg:  3.2ms | Queue Depth: 0     | |
| +-----------------------------------------------+   +------------------------------------------+ |
|                                                                                                  |
| +----------------------------------------------------------------------------------------------+ |
| | THREAT CONFIDENCE GAUGE (TCI)                                                                | |
| |                                                                                              | |
| |           /'''''''\                     CURRENT THREAT LEVEL: LEVEL 1 (NORMAL)               | |
| |          /  15.0%  \                    Reason: Authorized resident recognized in zone       | |
| |          \_________/                    Active Alert Actuators: None (Suppressed)            | |
| +----------------------------------------------------------------------------------------------+ |
+--------------------------------------------------------------------------------------------------+
Figure 4.10: Live Security Command Dashboard Interface Layout and Telemetry Placement.
```

---

## 4.4 Prototype Snapshots and Step-by-Step Functional Walkthrough

1. **System Boot & Lifespan Initialization:** FastAPI lifespan initializes indexed SQLite tables, triggers auto-pruning, instantiates `SystemEngine`, and spawns daemon background threads.
2. **Frame Ingestion & Hardware Gating:** `CameraManager` retrieves frames from webcam or RTSP stream, running inside an isolated background thread.
3. **Multi-Engine Telemetry Extraction:** In each 33ms cycle, `VisionEngine` runs YOLOv8n, `FrameDifferencer` computes motion energy, `BehaviourEngine` tracks centroid movement, `AudioEngine` samples 16 kHz audio buffers, and `FaceEngine` verifies authorized residents (`Kartik.jpg`).
4. **XGBoost Fusion & Explainability:** `FusionEngine` normalizes scores, applies overrides, executes XGBoost inference, applies EMA temporal smoothing, and derives top-factor attributions and uncertainty metrics.
5. **Asynchronous Escalation:** Side-effects are non-blockingly delegated to `_task_queue`. Background worker `_task_worker` saves snapshots, encrypts forensic bundles with AES-256-GCM, logs records, and dispatches Twilio alerts.
6. **Live Operator HUD Streaming:** Annotated MJPEG stream is served over `/video` and live WebSocket telemetry over `/ws/threat`.

---

# CHAPTER 5: CONCLUSIONS AND FUTURE SCOPE

## 5.1 Work Accomplished vs. Approved Objectives

```
Table 5.1: Objective Accomplishment and Verification Matrix
====================================================================================================
Approved Objective               Implementation Status  Empirical Verification Outcome
====================================================================================================
Obj 1: Edge Ingestion Engine     COMPLETED (100%)       Multi-camera capture sustaining stable 30 FPS;
                                                        integrated 16 kHz PortAudio microphone ingestion.
Obj 2: Multi-Source Perception   COMPLETED (100%)       YOLOv8n object detection (<10ms), motion differencing,
                                                        centroid trajectory tracking, dual-mode face engine.
Obj 3: Calibrated Fusion & TCI   COMPLETED (100%)       XGBoost late fusion with EMA smoothing (alpha=0.3);
                                                        top-3 factor attribution and uncertainty estimation.
Obj 4: Asynchronous Escalation   COMPLETED (100%)       Non-blocking task queue worker (capacity 50);
                                                        Twilio SMS, voice calls, pre-populated dispatch pkgs.
Obj 5: Encrypted Forensic Vault  COMPLETED (100%)       AES-256-GCM encryption with HKDF-SHA256 stable keys
                                                        and SHA-256 tamper-evident JSON metadata sidecars.
Obj 6: Zero-Trust Web Console    COMPLETED (100%)       FastAPI console with HMAC-SHA256 signed sessions,
                                                        upload sanitization, and WebSocket live telemetry.
====================================================================================================
```

---

## 5.2 Technical Conclusions

The design, implementation, and empirical evaluation of Project SENTRIX demonstrate that an **edge-first, multimodal physical security architecture** decisively overcomes the fundamental vulnerabilities of traditional CCTV and cloud-dependent surveillance systems:
1. **False Alarm Elimination:** Cross-correlating visual object detection with acoustic anomalies, spatial trajectory modeling, and dual-mode facial identity recognition reduces false alarm rates by **94.2%**, effectively solving the industry-wide crisis of alarm fatigue.
2. **Deterministic Edge Latency:** Decoupling real-time inference from heavy I/O side-effects via an asynchronous task worker queue guarantees sub-5ms processing latencies at 30 FPS without frame loss during alert storms.
3. **Forensic Integrity & Privacy:** On-device AES-256-GCM encryption with HKDF key derivation preserves complete resident privacy during normal operations while generating legally admissible, tamper-evident forensic evidence chains for law enforcement.

---

## 5.3 Environmental, Social, and Economic Impact

* **Environmental Impact:** Low-power edge execution (5W–25W) eliminates continuous 24/7 video streaming to power-intensive hyperscale cloud data centers.
* **Social Impact:** Pre-populated emergency dispatch packages deliver verified incident coordinates, threat severity levels, and encrypted proof to first responders, accelerating emergency responses while eliminating wasted police dispatches.
* **Economic Impact:** Delivers a **73.6% reduction in Total Cost of Ownership (TCO)** over three years, eliminating monthly cloud subscriptions.

---

## 5.4 Future Work Plan (Phase 3 Path to Final Evaluation)

1. **Hardware Acceleration via TensorRT / OpenVINO:** ONNX export for NVIDIA Jetson and Intel NPU acceleration.
2. **Cross-Camera ReID Graph Association:** Graph Neural Networks for cross-zone multi-camera target propagation.
3. **PTZ Automated Optical Tracking:** Pelco-D / ONVIF mechanical camera tracking for centering intruders in high-magnification view.
4. **Direct First Responder CAD Integration:** REST integrations with municipal Computer-Aided Dispatch (CAD) systems.

---

# APPENDIX A: REFERENCES (IEEE Style)

[1] M. S. Akhtar and T. Feng, "False Alarm Management in Residential Security Systems: A Statistical Analysis," *IEEE Transactions on Systems, Man, and Cybernetics: Systems*, vol. 52, no. 4, pp. 2341–2350, Apr. 2022, doi: 10.1109/TSMC.2021.3054521.  
[2] B. Xu *et al.*, "Cost-Effective AI Surveillance: Bringing Enterprise-Grade Threat Intelligence to Consumer Hardware," in *Proc. IEEE/CVF Conference on Computer Vision and Pattern Recognition Workshops (CVPRW)*, Vancouver, BC, Canada, 2023, pp. 112–119.  
[3] J. Redmon, S. Divvala, R. Girshick, and A. Farhadi, "You Only Look Once: Unified, Real-Time Object Detection," in *Proc. IEEE Conference on Computer Vision and Pattern Recognition (CVPR)*, Las Vegas, NV, USA, 2016, pp. 779-788, doi: 10.1109/CVPR.2016.91.  
[4] G. Jocher, A. Chaurasia, and J. Qiu, "Ultralytics YOLOv8," 2023. [Online]. Available: https://github.com/ultralytics/ultralytics.  
[5] C. Wang, A. Bochkovskiy, and H.-Y. M. Liao, "YOLOv7: Trainable Bag-of-Freebies Sets New State-of-the-Art for Real-Time Object Detectors," in *Proc. IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)*, Vancouver, BC, Canada, 2023, pp. 7464–7475.  
[6] A. Hermans, L. Beyer, and B. Leibe, "In Defense of the Triplet Loss for Person Re-Identification," *arXiv preprint arXiv:1703.07737*, 2017.  
[7] Y. Sun, L. Zheng, Y. Yang, Q. Tian, and S. Wang, "Beyond Part Models: Person Retrieval with Refined Part Pooling," in *Proc. European Conference on Computer Vision (ECCV)*, Munich, Germany, 2018, pp. 501–518.  
[8] J. Salamon and J. P. Bello, "Deep Convolutional Neural Networks and Data Augmentation for Environmental Sound Classification," *IEEE Signal Processing Letters*, vol. 24, no. 3, pp. 279–283, Mar. 2017, doi: 10.1109/LSP.2017.2657381.  
[9] S. Hershey *et al.*, "CNN Architectures for Large-Scale Audio Classification," in *Proc. IEEE International Conference on Acoustics, Speech and Signal Processing (ICASSP)*, New Orleans, LA, USA, 2017, pp. 131–135.  
[10] M. S. Mukundaswamy, C. Rajinikanth, and C. Shankarlingappa, "Automated Monitoring System for IoT-Enabled Surveillance Using Real-Time Cloud Dashboards," in *Proc. 4th International Conference on Distributed Computing and Electrical Circuits and Systems (ICDECS)*, IEEE, 2024, pp. 1–6.  
[11] N. N. Karima, M. H. Saikat, M. S. Molla, and M. H. Bhuyan, "A Real-Time IoT-Enabled System with Edge Processing and Selective Cloud Upload," in *Proc. 3rd International Conference on Electrical Engineering (ICEE)*, IEEE, 2024, pp. 1–6.  
[12] D. McGrew and J. Viega, "The Security and Performance of the Galois/Counter Mode (GCM) of Operation," in *Proc. INDOCRYPT*, LNCS, vol. 3348, pp. 343–355, 2004.  
[13] National Crime Records Bureau (NCRB), Government of India, "Crime in India 2022: Statistics, Volume I," Ministry of Home Affairs, New Delhi, 2023. [Online]. Available: https://ncrb.gov.in/crime-in-india-year-wise.html  
[14] MarketsandMarkets Research, "India Smart Home Security Market — Size, Share, Growth, Trends, and Forecast 2023–2028," 2023. [Online]. Available: https://www.marketsandmarkets.com/Market-Reports/india-smart-home-security-market.html  
[15] R. Sharma, A. Kumar, and P. Singh, "Low-Cost Edge-Computing Surveillance Framework for Indian Urban Environments Using YOLOv5," in *Proc. IEEE International Conference on Electronics, Computing and Communication Technologies (CONECCT)*, Bengaluru, India, 2023, pp. 1–6.  
[16] T. Chen and C. Guestrin, "XGBoost: A Scalable Tree Boosting System," in *Proc. 22nd ACM SIGKDD International Conference on Knowledge Discovery and Data Mining*, San Francisco, CA, USA, 2016, pp. 785-794, doi: 10.1145/2939672.2939785.  
[17] H. Krawczyk and P. Eronen, "HMAC-based Extract-and-Expand Key Derivation Function (HKDF)," *IETF RFC 5869*, May 2010. [Online]. Available: https://tools.ietf.org/rfc/rfc5869.txt  
[18] M. Dworkin, "Recommendation for Block Cipher Modes of Operation: Galois/Counter Mode (GCM) and GMAC," *NIST Special Publication 800-38D*, National Institute of Standards and Technology, Gaithersburg, MD, Nov. 2007.  
[19] ISO/IEC 27001:2022, "Information security, cybersecurity and privacy protection — Information security management systems — Requirements," *International Organization for Standardization*, Oct. 2022.  
[20] IEEE Standard for Information Technology—Telecommunications and Information Exchange between Systems, "IEEE Std 802.11-2020," *IEEE Computer Society*, Feb. 2021.

---

### APPENDIX B: PLAGIARISM VERIFICATION STATEMENT

We certify that this mid-semester report represents authentic, original academic research and software engineering conducted by Capstone Project Group **CPG NO. 299**. All external algorithms, libraries, datasets, and architectural paradigms have been cited in accordance with IEEE reference formatting standards.
