import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { OB } from "@/pages/onboardingTheme";
import { WalkthroughFlow, type WalkthroughExit } from "./WalkthroughFlow";

// The first-email walkthrough as a pop-up on Find. Every way out (the X and
// Escape included) goes through onExit; closing without a choice counts as
// "later". A click outside does nothing, so a stray click can't close it.

interface WalkthroughDialogProps {
  open: boolean;
  onExit: (why: WalkthroughExit) => void;
}

export function WalkthroughDialog({ open, onExit }: WalkthroughDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onExit("later")}>
      <DialogContent
        aria-describedby={undefined}
        onInteractOutside={(e) => e.preventDefault()}
        className="block max-h-[92vh] w-[calc(100vw-24px)] max-w-[1040px] overflow-y-auto p-0 sm:rounded-[20px]"
        style={{ background: OB.pageBg }}
      >
        <DialogTitle className="sr-only">Your first email</DialogTitle>
        <WalkthroughFlow mode="modal" gmailReturnPath="/find?walkthrough=1" onExit={onExit} />
      </DialogContent>
    </Dialog>
  );
}
