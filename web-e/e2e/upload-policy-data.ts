export const uploadPolicy = {
  chat_image_max_bytes: 10 * 1024 * 1024,
  chat_file_max_bytes: 50 * 1024 * 1024,
  workspace_file_max_bytes: 50 * 1024 * 1024,
  image_extensions: ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'],
} as const
