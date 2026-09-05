import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { PANES } from "./panes";

export default function App() {
  return (
    <div className="min-h-full bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 px-6 py-4">
        <h1 className="text-xl font-semibold tracking-tight">Trade Frame</h1>
        <p className="text-sm text-slate-400">Single pane of glass</p>
        <nav className="mt-3 flex gap-4 text-sm">
          {PANES.map((pane) => (
            <NavLink
              key={pane.path}
              to={pane.path}
              className={({ isActive }) =>
                isActive
                  ? "font-medium text-slate-100"
                  : "text-slate-400 hover:text-slate-200"
              }
            >
              {pane.label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="w-full px-6 py-8">
        <Routes>
          <Route path="/" element={<Navigate to="/watchlist" replace />} />
          {PANES.map((pane) => (
            <Route key={pane.path} path={pane.path} element={<pane.Component />} />
          ))}
        </Routes>
      </main>
    </div>
  );
}
