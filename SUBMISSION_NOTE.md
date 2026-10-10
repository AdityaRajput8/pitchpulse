PitchPulse: a live copilot for your Dream11 team

The problem: once a team locks, fans spend the match watching a rank that jumps by thousands with no explanation. They can't tell which ball hurt them, whether they can still recover, or what they missed while away, and notifications ping on every wicket, relevant or not.

How it helps: PitchPulse tracks your XI ball by ball against a 20,000-team contest and explains every swing ("Down 771 ranks. Dhoni took a catch; he's in 71% of teams and you don't have him"). It shows which of your players can still move you up, catches you up in one paragraph when you return to the tab, and only alerts on moments that affect your team, filtering about 89% of balls as noise.

Under the hood: event-driven FastAPI (Redis Streams → worker → WebSocket), Dream11 T20 scoring and vectorised ranking under 1 ms per ball. The engine computes every number; the LLM only narrates, and a guard rejects any sentence containing a number not in the facts. The demo replays a simulated IPL match; real Cricsheet files drop in.

AI tools: Google Gemini (narration, with Groq and Claude fallbacks) and Claude (pair-programming and design review).
