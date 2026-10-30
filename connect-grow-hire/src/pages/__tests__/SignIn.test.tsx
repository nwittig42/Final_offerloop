import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const signUpWithEmail = vi.fn(async () => "onboarding" as const);
const signInWithEmail = vi.fn(async () => "home" as const);

vi.mock("@/contexts/FirebaseAuthContext", () => ({
  useFirebaseAuth: () => ({
    user: null,
    isLoading: false,
    signIn: vi.fn(),
    signInWithApple: vi.fn(),
    signUpWithEmail,
    signInWithEmail,
    resetPassword: vi.fn(),
  }),
  friendlyAuthError: () => "error",
}));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));

import SignIn from "../SignIn";

const renderSignUp = () =>
  render(
    <MemoryRouter initialEntries={["/signin?mode=signup"]}>
      <SignIn />
    </MemoryRouter>,
  );

const fill = (label: string, value: string) =>
  fireEvent.change(screen.getByPlaceholderText(label), { target: { value } });

describe("SignIn — create account", () => {
  it("won't create an account until both passwords match", () => {
    renderSignUp();
    fill("Full name", "Test User");
    fill("Email address", "nick+test@offerloop.ai");
    fill("Password", "secret123");
    fill("Confirm password", "secret12");
    expect(screen.getByRole("alert")).toHaveTextContent("Passwords don't match.");
    const form = screen.getByPlaceholderText("Password").closest("form")!;
    const submit = form.querySelector('button[type="submit"]') as HTMLButtonElement;
    expect(submit).toBeDisabled();
    fireEvent.submit(form);
    expect(signUpWithEmail).not.toHaveBeenCalled();

    fill("Confirm password", "secret123");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(submit).toBeEnabled();
    fireEvent.submit(form);
    expect(signUpWithEmail).toHaveBeenCalledWith("Test User", "nick+test@offerloop.ai", "secret123");
  });

  it("the eye shows and hides both password fields", () => {
    renderSignUp();
    const pw = screen.getByPlaceholderText("Password");
    const confirm = screen.getByPlaceholderText("Confirm password");
    expect(pw).toHaveAttribute("type", "password");
    fireEvent.click(screen.getAllByRole("button", { name: "Show password" })[0]);
    expect(pw).toHaveAttribute("type", "text");
    expect(confirm).toHaveAttribute("type", "text");
    fireEvent.click(screen.getAllByRole("button", { name: "Hide password" })[0]);
    expect(pw).toHaveAttribute("type", "password");
  });

  it("sign in has no confirm field but still has the eye", () => {
    render(
      <MemoryRouter initialEntries={["/signin"]}>
        <SignIn />
      </MemoryRouter>,
    );
    expect(screen.queryByPlaceholderText("Confirm password")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show password" })).toBeInTheDocument();
  });
});
