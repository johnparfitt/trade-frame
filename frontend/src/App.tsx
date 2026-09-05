import Watchlist from "./components/Watchlist";

export default function App() {
  return (
    <div className="min-h-full bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 px-6 py-4">
        <h1 className="text-xl font-semibold tracking-tight">Trade Frame</h1>
        <p className="text-sm text-slate-400">Single pane of glass — Watchlist</p>
      </header>
      <main className="w-full px-6 py-8">
        <Watchlist />
      </main>
    </div>
  );
}
