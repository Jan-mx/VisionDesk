import { useState } from 'react'
import { TextInput, type TextInputProps } from 'react-admin'
import { useWatch } from 'react-hook-form'

/**
 * 专用于列表筛选的日期选择组件：
 * - 默认无值且未聚焦时保持 type="text"，清除浏览器原生 type="date" 强制显示的占位格式（如 yyyy/mm/dd 或 年/月/日）；
 * - 此时仅展示纯净的参数说明标签（如「创建起始日期」）；
 * - 聚焦或已有选定值时平滑切换为 type="date"，显示日历图标并支持原生日期选择。
 */
export function DateFilterInput(props: TextInputProps) {
  const { source, onFocus, onBlur, ...rest } = props
  const formValue = useWatch({ name: source })
  const [focused, setFocused] = useState(false)

  const isDate = Boolean(formValue || focused)

  return (
    <TextInput
      source={source}
      {...rest}
      type={isDate ? 'date' : 'text'}
      onFocus={(e) => {
        setFocused(true)
        onFocus?.(e)
      }}
      onBlur={(e) => {
        setFocused(false)
        onBlur?.(e)
      }}
      onClick={() => {
        if (!isDate) {
          setFocused(true)
        }
      }}
    />
  )
}
