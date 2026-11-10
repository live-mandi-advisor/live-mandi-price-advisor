import axios from "axios";

const api = axios.create({
  baseURL: "/api",
  timeout: 30000,
  headers: { Accept: "application/json" }
});

export async function getStates() {
  const { data } = await api.get("/states");
  return data;
}

export async function getTopCrops(state) {
  const { data } = await api.get(`/states/${encodeURIComponent(state)}/top-crops`);
  return data;
}

export async function getStateSummary(state) {
  const { data } = await api.get(`/states/${encodeURIComponent(state)}/summary`);
  return data;
}

export const getHistory = (commodity, market) =>
  api.get("/history", { params: { commodity, market } }).then((r) => r.data);

export const getForecast = (commodity, market) =>
  api.get("/forecast", { params: { commodity, market } }).then((r) => r.data);
