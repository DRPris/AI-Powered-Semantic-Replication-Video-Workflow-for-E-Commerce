"""
商品属性提取 Prompt 模板
用于分析产品图片，提取商品属性
"""

PRODUCT_ANALYSIS_PROMPT = """Analyze this product image for video replication purposes.
I need to understand this product well enough to replace it in an existing video while keeping the video structure intact.

Output the following:

─────────────────────────────────
## LAYER 0: COMPONENT DECOMPOSITION
(CRITICAL for multi-component / combo products)

First, determine if this product is a SINGLE item or a COMBINATION of separable components.
- is_combo_product: [true / false]
- If true, list EACH separable component individually:

For EACH component:
- component_name: [e.g., "bar soap", "mesh pouch", "drawstring cord"]
- shape: [describe this specific component's 3D form independently]
- color: [this component's OWN color — do NOT blend with other components]
- material: [e.g., "solid soap", "polyester mesh netting", "nylon cord"]
- texture: [surface texture of THIS component only]
- role: ["primary_product" / "container" / "accessory" / "attachment"]
- belongs_to: [which component this is physically attached to, or "standalone"]

Component relationships:
- containment: [which component goes INSIDE which? e.g., "soap goes inside mesh pouch"]
- attachment: [which features belong to which component? e.g., "drawstring cord is part of mesh pouch, NOT the soap"]
- size_relationship: [relative sizes, e.g., "pouch is slightly larger than soap to contain it"]

For container/pouch/bag/box type components, also describe:
- opening_location: [where is the opening? top / side / bottom / end]
- opening_direction: [which direction does the opening face when held naturally?]
- insertion_method: [how are items placed inside? e.g., "soap slides in through the top narrow opening", "lid lifts off to reveal cavity"]
- closure_mechanism: [how does it close? drawstring / zipper / snap / fold-over / none]

IMPORTANT:
- Do NOT attribute features of one component to another (e.g., if only the pouch has a cord, the soap does NOT have a cord)
- Describe each component's color INDEPENDENTLY — do NOT mix or average colors across components
- Each component's shape must be described based on THAT component alone

## LAYER 1: PHYSICAL ATTRIBUTES
(affects shot composition and framing — describes the product AS A WHOLE)

- Overall shape: [describe the 3D form of the assembled/combined product]
- Orientation in use: [vertical / horizontal / varies]
- Size relative to human hand: [fits in one hand / two hands / etc.]
- Which part faces the camera naturally when held?
- Color and most visually prominent feature? (For texture descriptions: accurately describe the layer structure — single-layer vs. multi-layer — and any elastic/stretchable properties. Do NOT use approximate metaphors like "honeycomb-like" when the actual structure is different, e.g. a multi-layer expandable mesh.)
- Any moving parts visible from outside?

## LAYER 2: OPERATION MECHANICS
(affects human action in the video)

- How is it held? [dominant hand position, grip type]
- What body part activates it? [thumb / squeeze whole hand / press button / twist / etc.]
- Where is the activation point on the product? [top / bottom / side / specific button location]
- What is the activation motion? [press down / squeeze inward / twist clockwise / etc.]
- What happens visually when activated? [water flows / light turns on / opens up / etc.]
- How long does the activation take? [instant / hold for 2s / etc.]
- One-handed or two-handed operation?

## LAYER 3: USE EFFECT
(affects what the camera captures as the "payoff" moment)

- What is the visible result of using this product? [water appears in bowl / stream of water / product opens / etc.]
- Where does this result appear relative to the product? [above / below / front / side]
- How does the target user (pet/person) interact with the result? [dog drinks from bowl that forms / cat licks water stream / etc.]
- Duration of the visible effect: [continuous / momentary / etc.]

## LAYER 4: SHOT CONSTRAINTS
(tells the replication AI what it cannot change)

Based on the above, answer:
- Minimum shot size needed to show the operation clearly? [must be at least medium close-up to show button press / etc.]
- Camera angle that best shows the key action?
- What must be IN FRAME at the same time? [hand + product + water output must all be visible]
- What is the single most important visual moment to capture? [the moment water flows into the trough]

## LAYER 5: COMPARISON TO ORIGINAL PRODUCT
(only fill this in when you have the original product info)

Original product operation: [describe]
New product operation: [describe]

For each shot in the original video:
- Can the same action be performed with the new product? Y/N
- If N: what is the closest equivalent action?
- What must change in the framing to accommodate this?
─────────────────────────────────

Please output your response in valid JSON format with the following structure:
{
  "layer_0_component_decomposition": {
    "is_combo_product": true,
    "components": [
      {
        "component_name": "...",
        "shape": "...",
        "color": "...",
        "material": "...",
        "texture": "...",
        "role": "primary_product / container / accessory / attachment",
        "belongs_to": "standalone / [parent component name]"
      }
    ],
    "relationships": {
      "containment": "...",
      "attachment": "...",
      "size_relationship": "..."
    },
    "container_structure": {
      "opening_location": "top / side / bottom / end (if applicable)",
      "opening_direction": "...",
      "insertion_method": "...",
      "closure_mechanism": "drawstring / zipper / snap / fold-over / none"
    }
  },
  "layer_1_physical_attributes": {
    "overall_shape": "...",
    "orientation_in_use": "...",
    "size_relative_to_hand": "...",
    "camera_facing_part": "...",
    "color_and_prominent_feature": "...",
    "moving_parts": "..."
  },
  "layer_2_operation_mechanics": {
    "how_held": "...",
    "activating_body_part": "...",
    "activation_point_location": "...",
    "activation_motion": "...",
    "visual_result_when_activated": "...",
    "activation_duration": "...",
    "one_or_two_handed": "..."
  },
  "layer_3_use_effect": {
    "visible_result": "...",
    "result_position_relative_to_product": "...",
    "target_user_interaction": "...",
    "effect_duration": "..."
  },
  "layer_4_shot_constraints": {
    "minimum_shot_size": "...",
    "best_camera_angle": "...",
    "must_be_in_frame": "...",
    "most_important_visual_moment": "..."
  },
  "layer_5_comparison": {
    "original_product_operation": "...",
    "new_product_operation": "...",
    "shot_compatibility": [...]
  }
}
"""
