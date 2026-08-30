import { useEffect, useState } from 'react';
import { AutoComplete, Input } from 'antd';
import type { CSSProperties, ReactNode } from 'react';
import type { InputProps } from 'antd';

const SEASON_OPTIONS = Array.from({ length: 10 }, (_, season) => ({
  value: String(season),
  label: String(season),
}));

interface SeasonInputProps {
  value: number | null;
  onChange: (value: number | null) => void;
  placeholder?: string;
  size?: InputProps['size'];
  style?: CSSProperties;
  addonBefore?: ReactNode;
}

/** Season picker with common 0-9 choices and unrestricted numeric entry. */
export default function SeasonInput({
  value,
  onChange,
  placeholder,
  size,
  style,
  addonBefore,
}: SeasonInputProps) {
  const [text, setText] = useState(value == null ? '' : String(value));

  useEffect(() => {
    setText(value == null ? '' : String(value));
  }, [value]);

  const update = (next: string) => {
    if (!/^\d*$/.test(next)) return;
    setText(next);
    onChange(next === '' ? null : Number.parseInt(next, 10));
  };

  return (
    <AutoComplete
      value={text}
      options={SEASON_OPTIONS}
      onChange={update}
      onSelect={update}
      allowClear
      onClear={() => update('')}
      style={style}
      filterOption={(input, option) => option?.value.startsWith(input) ?? false}
    >
      <Input
        size={size}
        placeholder={placeholder}
        inputMode="numeric"
        addonBefore={addonBefore}
      />
    </AutoComplete>
  );
}
