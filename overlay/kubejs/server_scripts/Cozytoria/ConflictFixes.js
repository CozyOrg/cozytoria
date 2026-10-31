ServerEvents.recipes(e => {
  // Immersive Vehicles Official Content Pack
  // Copper Wire conflicts with 'createdeco:copper_support'
  e.shaped('8x mts:mtsofficialpack.copperwire', ['I I', '   ', 'I I'], {
    I: '#c:ingots/copper',
  }).id('mtsofficialpack:copperwire');
});
