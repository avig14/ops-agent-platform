import { Navigate, Route, Routes } from "react-router";
import { RequireAuth, RequireRole } from "./components/Auth";
import { Shell } from "./components/Shell";
import { AdminPage } from "./pages/Admin";
import { CaseDetailPage } from "./pages/CaseDetail";
import { DashboardPage } from "./pages/Dashboard";
import { HowItWorksPage } from "./pages/HowItWorks";
import { InboxPage } from "./pages/Inbox";
import { LoginPage } from "./pages/Login";
import { MailboxPage } from "./pages/Mailbox";
import { NewRequestPage } from "./pages/NewRequest";
import { NotFoundPage } from "./pages/NotFound";

const CAN_CREATE = ["requester", "manager", "regional_head", "finance_director"] as const;
const CAN_REPORT = ["manager", "regional_head", "finance_director", "admin"] as const;
const ADMIN_ONLY = ["admin"] as const;

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <Shell />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/inbox" replace />} />
        <Route path="/inbox" element={<InboxPage />} />
        <Route path="/cases" element={<Navigate to="/inbox" replace />} />
        <Route path="/cases/:id" element={<CaseDetailPage />} />
        <Route path="/mailbox" element={<MailboxPage />} />
        <Route path="/how-it-works" element={<HowItWorksPage />} />
        <Route element={<RequireRole roles={CAN_CREATE} />}>
          <Route path="/new" element={<NewRequestPage />} />
        </Route>
        <Route element={<RequireRole roles={CAN_REPORT} />}>
          <Route path="/dashboard" element={<DashboardPage />} />
        </Route>
        <Route element={<RequireRole roles={ADMIN_ONLY} />}>
          <Route path="/admin" element={<AdminPage />} />
        </Route>
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
