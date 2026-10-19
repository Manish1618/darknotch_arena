// Load test for an event day. Run it from another machine against a staging copy:
//
//   k6 run -e BASE=https://ctf.example.org -e EVENT=autumn-breach loadtest/k6.js
//   k6 run -e BASE=... -e EVENT=... -e PLAYERS=players.json -e VIEWERS=1200 -e PLAYERS_VUS=300 loadtest/k6.js
//
// Viewers poll the public scoreboard every 4 s, like the page does. With a
// players.json ([{"email": "...", "password": "..."}], test accounts on a
// staging event, never real ones) each player also logs in, opens the
// challenge board and polls team alerts every 3 s. The run fails if p95
// latency or the error rate crosses the thresholds below.
import http from "k6/http";
import { check, sleep } from "k6";
import { SharedArray } from "k6/data";

const BASE = __ENV.BASE || "http://localhost:8000";
const EVENT = __ENV.EVENT || "autumn-breach";
const VIEWERS = +(__ENV.VIEWERS || 1000);
const PLAYERS_VUS = +(__ENV.PLAYERS_VUS || 0);
const accounts = __ENV.PLAYERS ? new SharedArray("players", () => JSON.parse(open(__ENV.PLAYERS))) : [];

export const options = {
  scenarios: {
    viewers: { executor: "ramping-vus", exec: "viewer", startVUs: 0,
      stages: [{ duration: "2m", target: VIEWERS }, { duration: "5m", target: VIEWERS }, { duration: "1m", target: 0 }] },
    ...(accounts.length && PLAYERS_VUS ? { players: { executor: "ramping-vus", exec: "player", startVUs: 0,
      stages: [{ duration: "2m", target: PLAYERS_VUS }, { duration: "5m", target: PLAYERS_VUS }, { duration: "1m", target: 0 }] } } : {}),
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],                       // under 1% errors
    "http_req_duration{kind:board}": ["p(95)<500"],         // scoreboard p95 under 0.5 s
    "http_req_duration{kind:player}": ["p(95)<800"],
  },
};

export function viewer() {
  const r = http.get(`${BASE}/api/events/${EVENT}/scoreboard`, { tags: { kind: "board" } });
  check(r, { "board 200": (x) => x.status === 200 });
  sleep(4);
}

export function player() {
  const acct = accounts[(__VU - 1) % accounts.length];
  const jar = http.cookieJar();
  if (!jar.cookiesForURL(BASE)["__Host-session"] && !jar.cookiesForURL(BASE).session) {
    const r = http.post(`${BASE}/api/auth/login`, JSON.stringify(acct), {
      headers: { "Content-Type": "application/json", Origin: BASE }, tags: { kind: "player" } });
    if (!check(r, { "login 200": (x) => x.status === 200 })) { sleep(5); return; }
  }
  http.get(`${BASE}/api/events/${EVENT}/challenges`, { tags: { kind: "player" } });
  for (let i = 0; i < 10; i++) {
    const n = http.get(`${BASE}/api/events/${EVENT}/notices?after=0`, { tags: { kind: "player" } });
    check(n, { "notices 200": (x) => x.status === 200 });
    sleep(3);
  }
}
