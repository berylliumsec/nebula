import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from "react";
import type { HarnessSkillSummary } from "../api/types";

export interface HarnessSkillTokenRange {
  start: number;
  end: number;
  query: string;
}

/** Find the `$skill` token immediately before a composer caret. */
export function findHarnessSkillToken(value: string, caret: number): HarnessSkillTokenRange | undefined {
  const beforeCaret = value.slice(0, caret);
  const match = /(?:^|\s)\$([A-Za-z0-9._-]*)$/.exec(beforeCaret);
  if (!match) return undefined;
  const start = caret - match[1].length - 1;
  return { start, end: caret, query: match[1] };
}

export interface HarnessSkillAutocompleteProps {
  skills: HarnessSkillSummary[];
  token: HarnessSkillTokenRange;
  activeIndex: number;
  onActiveIndexChange: (index: number) => void;
  onSelect: (skill: HarnessSkillSummary) => void;
  onClose: () => void;
}

export function HarnessSkillAutocomplete({
  skills,
  token,
  activeIndex,
  onActiveIndexChange,
  onSelect,
  onClose,
}: HarnessSkillAutocompleteProps) {
  const normalized = token.query.toLocaleLowerCase();
  const matches = skills.filter((skill) => skill.name.toLocaleLowerCase().includes(normalized));
  if (!matches.length) {
    return <div className="chat-skill-menu chat-skill-menu-empty" role="status">No matching skills</div>;
  }

  const select = (event: ReactMouseEvent<HTMLButtonElement>, skill: HarnessSkillSummary) => {
    // Keep the textarea caret in place while the button is clicked/tapped.
    event.preventDefault();
    onSelect(skill);
  };

  return (
    <div className="chat-skill-menu" id="harness-skill-menu" role="listbox" aria-label="Available skills">
      <div className="chat-skill-menu-heading">
        <span>Skills</span>
        <small>↑↓ navigate · Enter select · Esc close</small>
      </div>
      {matches.map((skill, index) => (
        <button
          id={`harness-skill-option-${index}`}
          type="button"
          role="option"
          aria-selected={index === activeIndex}
          className={index === activeIndex ? "active" : undefined}
          key={skill.path}
          onMouseDown={(event) => select(event, skill)}
          onMouseEnter={() => onActiveIndexChange(index)}
          onKeyDown={(event: ReactKeyboardEvent<HTMLButtonElement>) => {
            if (event.key === "Escape") {
              event.preventDefault();
              onClose();
            }
          }}
        >
          <strong>${skill.name}</strong>
          <small>{skill.source === "project" ? "Project skill" : "Installed skill"}</small>
        </button>
      ))}
    </div>
  );
}
