import { AlertTriangle } from "lucide-react";

interface InlineValidationNoticeProps {
  message: string;
}

/** Local form feedback. It intentionally does not create or link to a diagnostic incident. */
export function InlineValidationNotice({ message }: InlineValidationNoticeProps) {
  return <p className="inline-validation-notice" role="alert"><AlertTriangle size={15} /> <span>{message}</span></p>;
}
