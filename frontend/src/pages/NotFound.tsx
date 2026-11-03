import { Link } from "react-router";
import { Empty } from "../components/ui";

export function NotFoundPage() {
  return (
    <div className="page page--narrow">
      <Empty
        title="That page does not exist"
        action={
          <Link to="/inbox" className="btn btn--primary">
            Go to the inbox
          </Link>
        }
      >
        The link may be old or mistyped.
      </Empty>
    </div>
  );
}
