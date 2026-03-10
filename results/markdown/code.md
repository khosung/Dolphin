```bash
# Handle different content types
  if label in {'sec_0', 'sec_1', 'sec_2', 'sec_3', 'sec_4', 'sec_5'}:
    markdown_content.append(self._handleHeading(text, label))
  elif label == 'fig':
    markdown_content.append(self._handle_figure(text, section_count))
  elif label == 'tab':
    print(text)
    markdown_content.append(self._handle_table(text))
    print(markdown_content[-1])
  elif label == 'equ':
    markdown_content.append(self._handle_formula(text))
  elif label == 'list':
    markdown_content.append(self._handle_list_item(text))
  elif label == 'code':
    markdown_content.append(f"```\nbash\n{text}\n```\n")
  elif label not in self.special_labels:
    # Handle regular text (paragraphs, etc.)
    processed_text = self._handle_text(text)
    markdown_content.append(f"{processed_text}\n\n")
  else:
    markdown_content.append(f"{text}\n\n")
  except Exception as e:
    print(f"Error processing item {section_count}: {str(e)}")
    # Add a placeholder for the failed item
    markdown_content.append(f"*[Error processing content]*\n\n")
```

