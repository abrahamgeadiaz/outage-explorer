import { NavLink, Outlet } from "react-router";

import { useSession } from "../auth/session";

export function Layout() {
  const session = useSession();
  const user = session.user;

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <span className="brand">Outage Explorer</span>
        <nav aria-label="Main">
          <NavLink to="/datasets">Datasets</NavLink>
          <NavLink to="/fleet">Fleet offline</NavLink>
          {session.can("query") && <NavLink to="/sql">SQL</NavLink>}
          {session.can("refresh") && <NavLink to="/refresh">Refresh</NavLink>}
        </nav>
        {user && (
          <div className="user">
            <span>
              {user.display_name} <span className={`role role-${user.role}`}>{user.role}</span>
            </span>
            <button type="button" className="link-button" onClick={() => session.logout()}>
              Sign out
            </button>
          </div>
        )}
      </header>
      <main id="main" className="content">
        <Outlet />
      </main>
    </div>
  );
}
