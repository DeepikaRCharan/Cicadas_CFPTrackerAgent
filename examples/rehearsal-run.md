# Cicadas demo

## Paper 1 of 1: ai-tutoring

**AI tutoring outperforms in-class active learning: an RCT introducing a novel research-based design in an authentic educational setting**

### 1. Reading the paper (agent judgment)

- **Read as:** Academic & Practitioner
- **Cues that drove that call:**
  - randomized, controlled trial measuring college students' learning
  - offers empirical evidence for the efficacy
  - novel research-based design in an authentic educational setting
  - Keywords: Randomized Controlled Trial, Physics Education, STEM Education
- **Reasoning:** The paper reports an RCT with empirical measurement of learning outcomes and perceptions, framed with rigorous experimental methodology and scoped evidence claims typical of education research. It is written for an audience of education researchers/academics familiar with controlled trial design and pedagogical theory, not trade press or policy audiences.

### 2. Searching (agent-directed)

_Each search and fetch as the model makes it. Call numbers skip its decisions -- those are under Verifying._
**Probes**
- **#1 Search:** AI Tutoring call for speakers
  - why: "Probe 1: Industry & Technical Media – searching for "AI Tutoring call for speakers""
  - 9 result(s); top: AI Voice Tutor - Talk to Your AI Tutor
- **#3 Search:** AI Tutoring policy forum
  - why: "Probe 2: Corporate & Institutional – searching for "AI Tutoring policy forum""
  - 9 result(s); top: UNESCO Policy Forum on Private Supplementary Tutoring Successfully Held in Paris (308 days ago)
- **#5 Search:** AI Tutoring open call for submissions
  - why: "Probe 3: Other Avenues – searching for "AI Tutoring open call for submissions""
  - 10 result(s); top: Open Source AI Model for Tutoring (EDU AI) Request for Proposals
- **Reason for #6-#7:** "Recording results for probe 3:"
- **Limit:** probe phase budget reached ($0.110 spend (spent $0.090; next turn projected $0.027)) -- the main searches keep their reserved budget
**Main searches**
- **#8 Search:** AI tutoring conference call for papers 2027
  - why: "Searching for AI tutoring conference calls 2027."
  - 9 result(s); top: Teaching and Learning with AI - Artificial Intelligence for All (347 days ago)
- **Reason for #9-#13:** "Recording arxiv papers as not_a_call (they're academic work, not venue calls)."
- **#14 Fetch:** https://aiforall.ucf.edu/teachwithai/
  - why: "Fetching Teaching and Learning with AI 2027 call page."
  - readable, 18,091 characters — Teaching and Learning with AI - Artificial Intelligence for All
- **Reason for #15-#16:** "Checking if Teaching and Learning with AI 2027 is already tracked."
- **#16 Fetch:** https://csedu.scitevents.org/callforpapers.aspx
  - result arrives with the next response
  - #16 result arrived: readable, 16,583 characters — CSEDU 2027 - callforpapers
- **Reason for #17-#18:** "Recording Teaching and Learning with AI as live and checking CSEDU status."
- **#18 Fetch:** https://www.icaife.net/call_for_paper
  - result arrives with the next response
  - #18 result arrived: readable, 4,998 characters — 2027 4th International Conference on Artificial Intelligence and Future Education（AIFE 2027)
- **#19 Search:** AIFE 2027 4th International Conference Artificial Intelligence Future Education deadline
  - why: "Searching for AIFE 2027 submission deadline and details."
  - 10 result(s); top: 2027 4th International Conference on Artificial Intelligence and Future Education（AIFE 2027)
- **Limit:** candidate limit (2) reached
_Running total: 22 steps | 10 model calls | 331,493 tokens | $0.185 spent_

### 3. Verifying (code-checked)

- **REJECTED** https://notilo.ai/tutor: not a call for submissions
- **REJECTED** https://iite.unesco.org/news/unesco-policy-forum-private-tutoring/: stale (past edition or closed call)
- **REJECTED** https://k12-ai-infrastructure.org/edu-ai-rfp/: stale (past edition or closed call)
- **REJECTED** https://humanityai.ai/open-call/: not a call for submissions
- **REJECTED** https://arxiv.org/pdf/2608.11259: not a call for submissions
- **REJECTED** https://arxiv.org/pdf/2605.05648: not a call for submissions
- **REJECTED** https://arxiv.org/pdf/2512.03688: not a call for submissions
- **REJECTED** https://arxiv.org/pdf/2507.12484: not a call for submissions
- **REJECTED** https://arxiv.org/pdf/2509.28615: not a call for submissions
- **ACCEPTED** Teaching and Learning with AI 2027 -- https://aiforall.ucf.edu/teachwithai/
  - deadline quote verified on the page: "The deadline to submit a proposal is 11:59p.m. EST on January 22, 2027."
- **ACCEPTED** CSEDU 2027 -- https://csedu.scitevents.org/callforpapers.aspx
  - deadline quote verified on the page: "Paper Submission: November 17, 2026"
- **VERIFIED** Teaching and Learning with AI 2027: complete, call open, deadline 11:59p.m. EST on January 22, 2027 -> scored
- **VERIFIED** CSEDU 2027: complete, call open, deadline November 17, 2026 -> scored

### 4. Scoring (agent judgment)

**Teaching and Learning with AI 2027**
- **Academic & Practitioner:** overall 51 (clears 50%)
  - Theme 62: The paper is an RCT comparing a custom AI tutor to in-class active learning for college students, centered...
  - Genre 45: The venue explicitly states it is "primarily a conference focused on sharing practices (rather than researc...
  - Format 45: The venue requires short proposals: an abstract capped at 100 words, plus short answers on transferability...
**CSEDU 2027**
- **Academic & Practitioner:** overall 90 (clears 50%)
  - Theme 94: The paper is a direct, strong match for CSEDU's core scope.
  - Genre 85: The paper is a randomized controlled trial ("we report a randomized, controlled trial measuring college stu...
  - Format 90: CSEDU's page states authors "can submit their work in the form of a complete paper or an abstract," accepti...
_Running total: 22 steps | 18 model calls | 408,397 tokens | $0.352 spent_

## Results

| Venue | Location | Paper | Bucket | Deadline | Theme | Genre | Format | Overall | Clears 50% |
|---|---|---|---|---|---:|---:|---:|---:|---|
| **CSEDU 2027** | Rome, Italy | ai-tutoring | Academic | November 17, 2026 | 94 | 85 | 90 | 90 | **YES** |
| **Teaching and Learning with AI 2027** | Orlando, Florida (Walt Disney World Dolphin Resort) | ai-tutoring | Academic | 11:59p.m. EST on January 22, 2027 | 62 | 45 | 45 | 51 | **YES** |
2 of 2 row(s) clear the 50% threshold.
Estimated cost of this run: about $0.35 (list prices).
**TOTALS: 22 steps, 18 model calls, 408,397 tokens, $0.352 spent (list prices; the Console bill is authoritative)**
