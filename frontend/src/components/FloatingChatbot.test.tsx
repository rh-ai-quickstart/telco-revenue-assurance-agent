import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { FloatingChatbot } from "@/components/FloatingChatbot";

it("opens the assistant and returns a placeholder response without a backend call", async () => {
  const user = userEvent.setup();
  const fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);

  render(<FloatingChatbot />);

  await user.click(screen.getByRole("button", { name: "Ask AI" }));
  expect(screen.getByRole("dialog", { name: "AI assistant" })).toBeInTheDocument();
  expect(
    screen.getByText(/placeholder because the AI backend is not connected/i),
  ).toBeInTheDocument();

  await user.type(screen.getByLabelText("Question"), "What is driving revenue risk?");
  await user.click(screen.getByRole("button", { name: "Send" }));

  const conversation = screen.getByRole("log");
  expect(within(conversation).getByText("What is driving revenue risk?")).toBeInTheDocument();
  expect(within(conversation).getByText(/Once available, it will answer/i)).toBeInTheDocument();
  expect(fetchSpy).not.toHaveBeenCalled();
});

it("fills a suggested question and closes with Escape", async () => {
  const user = userEvent.setup();

  render(<FloatingChatbot />);

  await user.click(screen.getByRole("button", { name: "Ask AI" }));
  await user.click(screen.getByRole("button", { name: "Which region needs attention first?" }));
  expect(screen.getByLabelText("Question")).toHaveValue("Which region needs attention first?");

  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "AI assistant" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Ask AI" })).toHaveFocus();
});
