import { render, screen } from "@testing-library/react";
import { DashboardPage } from "@/pages/DashboardPage";

it("renders the complete frontend-only dashboard", () => {
  render(<DashboardPage />);

  expect(screen.getByRole("heading", { name: "Telco revenue assurance" })).toBeInTheDocument();
  expect(screen.getAllByText("$2.4M").length).toBeGreaterThan(0);
  expect(screen.getByText("18.4%")).toBeInTheDocument();
  expect(screen.getByText("6.7%")).toBeInTheDocument();
  expect(
    screen.getByRole("img", { name: /Monthly revenue increased from 9\.1 million/i }),
  ).toBeInTheDocument();
  expect(screen.getByRole("table")).toBeInTheDocument();
  expect(screen.getByText("Southeast")).toBeInTheDocument();
});
