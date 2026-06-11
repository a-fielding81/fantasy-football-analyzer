import axios from "axios";

const client = axios.create({ baseURL: "http://localhost:8000/api" });

export const api = {
  seasons: {
    list: () => client.get("/seasons/").then((r) => r.data),
    standings: (year: number) =>
      client.get(`/seasons/${year}/standings`).then((r) => r.data),
    weeklyScores: (year: number) =>
      client.get(`/seasons/${year}/weekly-scores`).then((r) => r.data),
  },
  teams: {
    list: () => client.get("/teams/").then((r) => r.data),
    history: (name: string) =>
      client.get(`/teams/${encodeURIComponent(name)}/history`).then((r) => r.data),
  },
  trades: {
    detail: (year?: number) =>
      client
        .get("/trades/detail", { params: year ? { year } : {} })
        .then((r) => r.data),
    grades: (year?: number) =>
      client
        .get("/trades/grades", { params: year ? { year } : {} })
        .then((r) => r.data),
    byManager: (name: string) =>
      client.get(`/trades/managers/${encodeURIComponent(name)}`).then((r) => r.data),
  },
  draft: {
    summary: (year?: number) =>
      client
        .get("/draft/", { params: year ? { year } : {} })
        .then((r) => r.data),
    keepers: (year?: number) =>
      client
        .get("/draft/keepers", { params: year ? { year } : {} })
        .then((r) => r.data),
    valueOverAdp: (year?: number) =>
      client
        .get("/draft/value-over-adp", { params: year ? { year } : {} })
        .then((r) => r.data),
  },
  players: {
    search: (q: string) =>
      client.get("/players/search", { params: { q } }).then((r) => r.data),
    history: (id: number) =>
      client.get(`/players/${id}/history`).then((r) => r.data),
    trades: (id: number) =>
      client.get(`/players/${id}/trades`).then((r) => r.data),
  },
};
