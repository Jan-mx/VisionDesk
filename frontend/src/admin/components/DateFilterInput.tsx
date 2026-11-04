import { useRef, useState } from 'react'
import { FieldTitle, useInput } from 'ra-core'
import { Box, InputAdornment, TextField } from '@mui/material'

export interface DateFilterInputProps {
  source: string
  label?: string
  alwaysOn?: boolean
  size?: 'small' | 'medium'
  className?: string
  defaultValue?: any
  [key: string]: any
}

/**
 * 专用于列表筛选的日期选择组件：
 * 1. 默认无值且未激活时保持 type="text"，彻底消除浏览器在未输入时强加的 yyyy/mm/dd 等占位文字，
 *    仅展示干净纯粹的参数说明标签（如「创建起始日期」、「创建截止日期」）；
 * 2. 用户点击该输入框任何区域（文本、标签、边框、日历小图标）时，在用户真实点击事件栈中直接同步切换并调用
 *    浏览器原生 showPicker()，直接弹出原生日期选择面板；
 * 3. 选定日期后正常显示选定值（如 2026-10-25），若清空且失焦则平滑退回文本形态。
 */
export function DateFilterInput(props: DateFilterInputProps) {
  const { source, label, size = 'small', className, alwaysOn: _alwaysOn, defaultValue, ...rest } = props
  const {
    id,
    field: { value, onChange, onBlur },
    fieldState: { invalid, error },
    isRequired,
  } = useInput({ source, defaultValue })

  const inputRef = useRef<HTMLInputElement>(null)
  const [isFocused, setIsFocused] = useState(false)
  const hasValue = Boolean(value)
  const showDate = hasValue || isFocused

  const openPicker = () => {
    setIsFocused(true)
    if (inputRef.current) {
      if (inputRef.current.type !== 'date') {
        inputRef.current.type = 'date'
      }
      try {
        inputRef.current.showPicker?.()
      } catch {
        // 针对不支持 showPicker 的老旧环境静默降级为常规焦点
      }
    }
  }

  const calendarIcon = (
    <InputAdornment position="end" sx={{ cursor: 'pointer', color: '#8d90a2', pointerEvents: 'none' }}>
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ width: 16, height: 16 }}
      >
        <rect width="18" height="18" x="3" y="4" rx="2" ry="2" />
        <line x1="16" y1="2" x2="16" y2="6" />
        <line x1="8" y1="2" x2="8" y2="6" />
        <line x1="3" y1="10" x2="21" y2="10" />
      </svg>
    </InputAdornment>
  )

  return (
    <Box
      onClick={openPicker}
      sx={{
        display: 'inline-block',
        cursor: 'pointer',
      }}
    >
      <TextField
        id={id}
        inputRef={inputRef}
        name={source}
        value={value ?? ''}
        type={showDate ? 'date' : 'text'}
        onChange={onChange}
        onFocus={() => {
          setIsFocused(true)
          if (inputRef.current && inputRef.current.type !== 'date') {
            inputRef.current.type = 'date'
          }
          try {
            inputRef.current?.showPicker?.()
          } catch {}
        }}
        onBlur={() => {
          setIsFocused(false)
          onBlur()
        }}
        onClick={(e) => {
          e.stopPropagation()
          openPicker()
        }}
        label={label ? <FieldTitle label={label} source={source} isRequired={isRequired} /> : null}
        size={size}
        className={className}
        error={invalid}
        helperText={invalid ? error?.message : null}
        InputLabelProps={{
          shrink: showDate ? true : undefined,
        }}
        InputProps={{
          endAdornment: calendarIcon,
        }}
        slotProps={{
          input: {
            endAdornment: calendarIcon,
          },
        }}
        sx={{
          cursor: 'pointer',
          '& .MuiInputBase-root': { cursor: 'pointer' },
          '& input': { cursor: 'pointer' },
        }}
        {...(rest as any)}
      />
    </Box>
  )
}
